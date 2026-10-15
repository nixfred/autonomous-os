package mqtthandler

import (
	"bytes"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"

	"github.com/gin-gonic/gin"

	"go.autonomous.ai/os/system/server/config"
)

const (
	testShop   = "my-store.myshopify.com"
	testSecret = "shpss_super_secret_value"
	testToken  = "shpat_minted_token_value"
)

// recordingTransport answers every request with a canned response and records
// where each one was sent, so a test can prove the credentials only went to the
// store host.
type recordingTransport struct {
	mu     sync.Mutex
	urls   []string
	forms  []url.Values
	status int
	body   string
	header http.Header
}

func (rt *recordingTransport) RoundTrip(r *http.Request) (*http.Response, error) {
	b, _ := io.ReadAll(r.Body)
	form, _ := url.ParseQuery(string(b))
	rt.mu.Lock()
	rt.urls = append(rt.urls, r.URL.String())
	rt.forms = append(rt.forms, form)
	rt.mu.Unlock()
	h := rt.header
	if h == nil {
		h = http.Header{"Content-Type": []string{"application/json"}}
	}
	return &http.Response{StatusCode: rt.status, Header: h, Body: io.NopCloser(strings.NewReader(rt.body))}, nil
}

type published struct {
	kind   string
	status string
	data   map[string]any
}

type shopifyTestEnv struct {
	h      *DeviceMQTTHandler
	router *gin.Engine
	dir    string
	rt     *recordingTransport
	pubs   *[]published
}

func newShopifyTestEnv(t *testing.T, rt *recordingTransport) *shopifyTestEnv {
	t.Helper()
	gin.SetMode(gin.TestMode)
	dir := t.TempDir()
	var pubs []published
	h := &DeviceMQTTHandler{
		config:          &config.Config{},
		connectorWriter: newConnectorWriter(dir, &fakeMCPGateway{}, nil),
		shopifyHTTP: &http.Client{
			Transport:     rt,
			CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse },
		},
		publishConnectorResult: func(kind, status, _ string, data interface{}) error {
			m, _ := data.(map[string]any)
			pubs = append(pubs, published{kind: kind, status: status, data: m})
			return nil
		},
	}
	r := gin.New()
	g := r.Group("/api/device/connectors")
	// Same registration order as the server: static shopify routes next to :code.
	g.GET("shopify", h.GetShopify)
	g.PUT("shopify", h.SetShopify)
	g.DELETE("shopify", h.RemoveShopify)
	g.GET(":code", h.GetConnector)
	g.DELETE(":code", h.RemoveConnector)
	return &shopifyTestEnv{h: h, router: r, dir: dir, rt: rt, pubs: &pubs}
}

func (e *shopifyTestEnv) do(method, path string, body any) *httptest.ResponseRecorder {
	var buf bytes.Buffer
	if body != nil {
		_ = json.NewEncoder(&buf).Encode(body)
	}
	req := httptest.NewRequest(method, path, &buf)
	req.Header.Set("Content-Type", "application/json")
	w := httptest.NewRecorder()
	e.router.ServeHTTP(w, req)
	return w
}

func okToken() *recordingTransport {
	return &recordingTransport{status: 200, body: `{"access_token":"` + testToken + `","scope":"write_products,write_content","expires_in":86399}`}
}

func goodBody() map[string]string {
	return map[string]string{"shop": testShop, "client_id": "cid-123", "client_secret": testSecret}
}

func assertNoSecrets(t *testing.T, what, s string) {
	t.Helper()
	for _, secret := range []string{testSecret, testToken} {
		if strings.Contains(s, secret) {
			t.Errorf("%s leaks a secret: %s", what, s)
		}
	}
}

func TestNormalizeShopifyShop(t *testing.T) {
	cases := []struct {
		in, want string
		ok       bool
	}{
		{"my-store.myshopify.com", "my-store.myshopify.com", true},
		{"  My-Store.MyShopify.com  ", "my-store.myshopify.com", true},
		{"https://my-store.myshopify.com/", "my-store.myshopify.com", true},
		{"http://my-store.myshopify.com/admin?x=1#f", "my-store.myshopify.com", true},
		{"my-store", "my-store.myshopify.com", true},
		{"admin.shopify.com/store/my-store", "", false},
		{"shop.example.com", "", false},
		{"evil.com/.myshopify.com", "", false},
		{"a@evil.com", "", false},
		{"169.254.169.254", "", false},
		{"my-store.myshopify.com.evil.com", "", false},
		{"", "", false},
		{"-bad.myshopify.com", "", false},
	}
	for _, c := range cases {
		got, err := normalizeShopifyShop(c.in)
		if (err == nil) != c.ok || got != c.want {
			t.Errorf("normalizeShopifyShop(%q) = %q, %v; want %q ok=%v", c.in, got, err, c.want, c.ok)
		}
	}
}

func TestSetShopify_HappyPath(t *testing.T) {
	e := newShopifyTestEnv(t, okToken())
	cache := filepath.Join(e.dir, shopifyTokenCacheFile)
	if err := os.WriteFile(cache, []byte(`{"stale":true}`), 0o600); err != nil {
		t.Fatal(err)
	}

	body := goodBody()
	body["shop"] = "  https://My-Store.myshopify.com/admin "
	w := e.do(http.MethodPut, "/api/device/connectors/shopify", body)
	if w.Code != http.StatusOK {
		t.Fatalf("status %d: %s", w.Code, w.Body.String())
	}
	assertNoSecrets(t, "response", w.Body.String())

	// The check went only to the store host.
	if len(e.rt.urls) != 1 || e.rt.urls[0] != "https://"+testShop+"/admin/oauth/access_token" {
		t.Fatalf("token request went to %v", e.rt.urls)
	}
	f := e.rt.forms[0]
	if f.Get("grant_type") != "client_credentials" || f.Get("client_id") != "cid-123" || f.Get("client_secret") != testSecret {
		t.Errorf("form = %v", f)
	}

	// Saved through the writer, in the same shape a cloud-linked connection has.
	creds, ok, err := e.h.connectorWriter.loadEntry("shopify")
	if err != nil || !ok {
		t.Fatalf("loadEntry: ok=%v err=%v", ok, err)
	}
	if creds.AuthType != "pat" || creds.Refresh || creds.AccessToken != testToken || creds.ExpiresAt == 0 {
		t.Errorf("entry = %+v", creds)
	}
	if creds.Credentials["shop"] != testShop || creds.Credentials["client_id"] != "cid-123" || creds.Credentials["client_secret"] != testSecret {
		t.Errorf("credentials = %+v", creds.Credentials)
	}

	// A stale helper cache is dropped so the new credentials are used.
	if _, err := os.Stat(cache); !os.IsNotExist(err) {
		t.Errorf("token cache should be removed, stat err = %v", err)
	}

	// The cloud is told the connector is connected — status only.
	if len(*e.pubs) != 1 || (*e.pubs)[0].kind != "connector.set.shopify" || (*e.pubs)[0].status != "success" {
		t.Fatalf("published = %+v", *e.pubs)
	}
	raw, _ := json.Marshal((*e.pubs)[0].data)
	assertNoSecrets(t, "published data", string(raw))
}

func TestSetShopify_InvalidInputSendsNothing(t *testing.T) {
	cases := map[string]map[string]string{
		"bad shop host":      {"shop": "evil.com", "client_id": "c", "client_secret": testSecret},
		"admin url":          {"shop": "admin.shopify.com/store/x", "client_id": "c", "client_secret": testSecret},
		"metadata ip":        {"shop": "169.254.169.254", "client_id": "c", "client_secret": testSecret},
		"userinfo":           {"shop": "a@evil.com", "client_id": "c", "client_secret": testSecret},
		"empty shop":         {"shop": "", "client_id": "c", "client_secret": testSecret},
		"missing client id":  {"shop": testShop, "client_secret": testSecret},
		"missing secret":     {"shop": testShop, "client_id": "c"},
		"secret has space":   {"shop": testShop, "client_id": "c", "client_secret": "has space"},
		"client id has tab":  {"shop": testShop, "client_id": "a\tb", "client_secret": testSecret},
		"secret too long":    {"shop": testShop, "client_id": "c", "client_secret": strings.Repeat("x", 300)},
		"secret has newline": {"shop": testShop, "client_id": "c", "client_secret": "a\nb"},
	}
	for name, body := range cases {
		t.Run(name, func(t *testing.T) {
			e := newShopifyTestEnv(t, okToken())
			w := e.do(http.MethodPut, "/api/device/connectors/shopify", body)
			if w.Code != http.StatusBadRequest {
				t.Fatalf("status %d: %s", w.Code, w.Body.String())
			}
			if len(e.rt.urls) != 0 {
				t.Errorf("a request was sent to %v", e.rt.urls)
			}
			if _, ok, _ := e.h.connectorWriter.loadEntry("shopify"); ok {
				t.Error("nothing may be saved for invalid input")
			}
			if len(*e.pubs) != 0 {
				t.Errorf("nothing may be published: %+v", *e.pubs)
			}
			if s := body["client_secret"]; len(s) >= 8 && len(s) < 50 && strings.Contains(w.Body.String(), s) {
				t.Errorf("response echoes the secret: %s", w.Body.String())
			}
		})
	}
}

func TestSetShopify_BadJSON(t *testing.T) {
	e := newShopifyTestEnv(t, okToken())
	req := httptest.NewRequest(http.MethodPut, "/api/device/connectors/shopify", strings.NewReader("{not json"))
	w := httptest.NewRecorder()
	e.router.ServeHTTP(w, req)
	if w.Code != http.StatusBadRequest || len(e.rt.urls) != 0 {
		t.Fatalf("status %d, requests %v", w.Code, e.rt.urls)
	}
}

func TestSetShopify_ProviderRejects(t *testing.T) {
	rt := &recordingTransport{status: 400, header: http.Header{"Content-Type": []string{"text/html"}},
		body: "<html>app_not_installed " + testSecret + "</html>"}
	e := newShopifyTestEnv(t, rt)
	w := e.do(http.MethodPut, "/api/device/connectors/shopify", goodBody())
	if w.Code != http.StatusBadRequest {
		t.Fatalf("status %d: %s", w.Code, w.Body.String())
	}
	if !strings.Contains(w.Body.String(), "Shopify rejected the client ID/secret or the app isn't installed on that store.") {
		t.Errorf("message = %s", w.Body.String())
	}
	assertNoSecrets(t, "response", w.Body.String())
	if strings.Contains(w.Body.String(), "<html>") {
		t.Errorf("response echoes the provider page: %s", w.Body.String())
	}
	if _, ok, _ := e.h.connectorWriter.loadEntry("shopify"); ok {
		t.Error("rejected credentials must not be saved")
	}
	if len(*e.pubs) != 0 {
		t.Errorf("nothing may be published: %+v", *e.pubs)
	}
}

func TestSetShopify_RedirectNotFollowedAndServerErrorsAreRetryable(t *testing.T) {
	for name, rt := range map[string]*recordingTransport{
		"redirect":     {status: 302, header: http.Header{"Location": []string{"https://evil.example/steal"}}},
		"server error": {status: 503, body: "down"},
		"no token":     {status: 200, body: `{"scope":"x"}`},
		"not json":     {status: 200, body: "<html>"},
	} {
		t.Run(name, func(t *testing.T) {
			e := newShopifyTestEnv(t, rt)
			w := e.do(http.MethodPut, "/api/device/connectors/shopify", goodBody())
			if w.Code != http.StatusBadGateway {
				t.Fatalf("status %d: %s", w.Code, w.Body.String())
			}
			if len(rt.urls) != 1 {
				t.Errorf("redirect was followed or retried: %v", rt.urls)
			}
			if _, ok, _ := e.h.connectorWriter.loadEntry("shopify"); ok {
				t.Error("nothing may be saved when the check could not complete")
			}
			assertNoSecrets(t, "response", w.Body.String())
		})
	}
}

func TestMintShopifyToken_LifetimeBounds(t *testing.T) {
	cases := map[string]int{
		`{"access_token":"t"}`:                      shopifyDefaultLifetime,
		`{"access_token":"t","expires_in":5}`:       shopifyMinLifetime,
		`{"access_token":"t","expires_in":86399}`:   86399,
		`{"access_token":"t","expires_in":9999999}`: shopifyMaxLifetime,
	}
	for body, want := range cases {
		rt := &recordingTransport{status: 200, body: body}
		tok, err := mintShopifyToken(t.Context(), &http.Client{Transport: rt}, testShop, "c", "s")
		if err != nil || tok.ExpiresIn != want {
			t.Errorf("%s: expires_in=%d err=%v, want %d", body, tok.ExpiresIn, err, want)
		}
	}
}

func TestGetShopify_NeverReturnsSecretOrToken(t *testing.T) {
	e := newShopifyTestEnv(t, okToken())

	// Not connected yet.
	w := e.do(http.MethodGet, "/api/device/connectors/shopify", nil)
	if w.Code != http.StatusOK || !strings.Contains(w.Body.String(), `"connected":false`) {
		t.Fatalf("status %d: %s", w.Code, w.Body.String())
	}

	if w := e.do(http.MethodPut, "/api/device/connectors/shopify", goodBody()); w.Code != http.StatusOK {
		t.Fatalf("connect: %d %s", w.Code, w.Body.String())
	}
	w = e.do(http.MethodGet, "/api/device/connectors/shopify", nil)
	assertNoSecrets(t, "GET shopify", w.Body.String())
	var env struct {
		Data shopifyInfoResponse `json:"data"`
	}
	if err := json.Unmarshal(w.Body.Bytes(), &env); err != nil {
		t.Fatalf("decode: %v (%s)", err, w.Body.String())
	}
	d := env.Data
	if !d.Connected || d.Shop != testShop || d.ClientID != "cid-123" || !d.HasSecret || d.ObtainedAt == 0 {
		t.Errorf("info = %+v", d)
	}
}

func TestGenericGetConnector_RedactsSecretCredentials(t *testing.T) {
	e := newShopifyTestEnv(t, okToken())
	if err := e.h.connectorWriter.Write(t.Context(), ConnectorCreds{
		Connector: "other", AuthType: "pat", APIKey: "pasted-key",
		Credentials: map[string]string{"page_id": "123", "client_secret": testSecret, "access_token": testToken, "Password": "p"},
	}); err != nil {
		t.Fatal(err)
	}
	w := e.do(http.MethodGet, "/api/device/connectors/other", nil)
	if w.Code != http.StatusOK {
		t.Fatalf("status %d", w.Code)
	}
	assertNoSecrets(t, "generic GET", w.Body.String())
	if strings.Contains(w.Body.String(), "pasted-key") || strings.Contains(w.Body.String(), `"p"`) {
		t.Errorf("generic GET leaks: %s", w.Body.String())
	}
	if !strings.Contains(w.Body.String(), `"page_id":"123"`) {
		t.Errorf("non-secret credential fields must still be returned: %s", w.Body.String())
	}
}

func TestRemoveShopify_CleansEntryAndCache(t *testing.T) {
	e := newShopifyTestEnv(t, okToken())
	if w := e.do(http.MethodPut, "/api/device/connectors/shopify", goodBody()); w.Code != http.StatusOK {
		t.Fatalf("connect: %d", w.Code)
	}
	cache := filepath.Join(e.dir, shopifyTokenCacheFile)
	if err := os.WriteFile(cache, []byte(`{"access_token":"cached"}`), 0o600); err != nil {
		t.Fatal(err)
	}
	*e.pubs = nil

	w := e.do(http.MethodDelete, "/api/device/connectors/shopify", nil)
	if w.Code != http.StatusOK || !strings.Contains(w.Body.String(), `"removed":true`) {
		t.Fatalf("status %d: %s", w.Code, w.Body.String())
	}
	if _, ok, _ := e.h.connectorWriter.loadEntry("shopify"); ok {
		t.Error("entry should be gone")
	}
	if _, err := os.Stat(cache); !os.IsNotExist(err) {
		t.Errorf("token cache should be removed, stat err = %v", err)
	}
	if len(*e.pubs) != 1 || (*e.pubs)[0].kind != "connector.remove.shopify" {
		t.Errorf("published = %+v", *e.pubs)
	}

	// Removing again is a clean no-op.
	w = e.do(http.MethodDelete, "/api/device/connectors/shopify", nil)
	if w.Code != http.StatusOK || !strings.Contains(w.Body.String(), `"removed":false`) {
		t.Errorf("second remove: %d %s", w.Code, w.Body.String())
	}
}

func TestReconnect_ReplacesCredentialsAndDropsOldCache(t *testing.T) {
	e := newShopifyTestEnv(t, okToken())
	if w := e.do(http.MethodPut, "/api/device/connectors/shopify", goodBody()); w.Code != http.StatusOK {
		t.Fatalf("connect: %d", w.Code)
	}
	cache := filepath.Join(e.dir, shopifyTokenCacheFile)
	_ = os.WriteFile(cache, []byte(`{"access_token":"old"}`), 0o600)

	body := goodBody()
	body["shop"] = "other-store"
	body["client_id"] = "cid-456"
	if w := e.do(http.MethodPut, "/api/device/connectors/shopify", body); w.Code != http.StatusOK {
		t.Fatalf("reconnect: %d", w.Code)
	}
	creds, _, _ := e.h.connectorWriter.loadEntry("shopify")
	if creds.Credentials["shop"] != "other-store.myshopify.com" || creds.Credentials["client_id"] != "cid-456" {
		t.Errorf("credentials = %+v", creds.Credentials)
	}
	if _, err := os.Stat(cache); !os.IsNotExist(err) {
		t.Error("old token cache must be dropped on reconnect")
	}
}

func TestPublicCredentials(t *testing.T) {
	if got := publicCredentials(nil); got != nil {
		t.Errorf("nil in, got %v", got)
	}
	got := publicCredentials(map[string]string{"client_secret": "a", "API_KEY": "b", "refresh-token": "c", "shop": "s", "page_id": "1"})
	if len(got) != 2 || got["shop"] != "s" || got["page_id"] != "1" {
		t.Errorf("got %v", got)
	}
	if got := publicCredentials(map[string]string{"client_secret": "a"}); got != nil {
		t.Errorf("only-secret map should become nil, got %v", got)
	}
}
