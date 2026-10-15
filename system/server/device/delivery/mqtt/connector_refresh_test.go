package mqtthandler

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"reflect"
	"testing"
	"time"

	"go.autonomous.ai/os/system/server/config"
)

func TestRefreshVarsFromCredentials(t *testing.T) {
	cases := []struct {
		name  string
		creds map[string]string
		want  map[string]string
	}{
		{name: "nil credentials", creds: nil, want: nil},
		{name: "no connector_vars", creds: map[string]string{"shop": "a.myshopify.com"}, want: nil},
		{name: "blank connector_vars", creds: map[string]string{credentialConnectorVars: "  "}, want: nil},
		{
			name:  "valid object",
			creds: map[string]string{credentialConnectorVars: `{"shop":"a.myshopify.com"}`},
			want:  map[string]string{"shop": "a.myshopify.com"},
		},
		{
			name:  "several vars",
			creds: map[string]string{credentialConnectorVars: `{"shop":"a.myshopify.com","region":"eu"}`},
			want:  map[string]string{"shop": "a.myshopify.com", "region": "eu"},
		},
		{name: "invalid json", creds: map[string]string{credentialConnectorVars: `{"shop":`}, want: nil},
		{name: "not an object", creds: map[string]string{credentialConnectorVars: `["a"]`}, want: nil},
		{name: "non-string value", creds: map[string]string{credentialConnectorVars: `{"shop":5}`}, want: nil},
		{name: "empty values dropped", creds: map[string]string{credentialConnectorVars: `{"shop":""}`}, want: nil},
		{
			name:  "empty value dropped, others kept",
			creds: map[string]string{credentialConnectorVars: `{"shop":"a.myshopify.com","x":""}`},
			want:  map[string]string{"shop": "a.myshopify.com"},
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if got := refreshVarsFromCredentials("demo", tc.creds); !reflect.DeepEqual(got, tc.want) {
				t.Fatalf("vars = %#v, want %#v", got, tc.want)
			}
		})
	}
}

// TestRequestConnectorTokenRefresh_Body proves vars ride in the refresh body only
// when the connector has some, so connectors with static endpoints send the same
// two-field body as before.
func TestRequestConnectorTokenRefresh_Body(t *testing.T) {
	cases := []struct {
		name     string
		vars     map[string]string
		wantVars map[string]any
	}{
		{name: "no vars", vars: nil, wantVars: nil},
		{name: "empty vars", vars: map[string]string{}, wantVars: nil},
		{name: "shop var", vars: map[string]string{"shop": "a.myshopify.com"}, wantVars: map[string]any{"shop": "a.myshopify.com"}},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			var gotPath string
			var gotBody map[string]any
			srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				gotPath = r.URL.Path
				raw, _ := io.ReadAll(r.Body)
				_ = json.Unmarshal(raw, &gotBody)
				w.Header().Set("Content-Type", "application/json")
				_, _ = w.Write([]byte(`{"access_token":"new","refresh_token":"rt2","expires_in":3600,"token_type":"Bearer"}`))
			}))
			defer srv.Close()

			h := &DeviceMQTTHandler{config: &config.Config{LLMBaseURL: srv.URL + "/v1", LLMAPIKey: "k", DeviceID: "d"}}
			res, err := h.requestConnectorTokenRefresh(context.Background(), "demo", "rt1", tc.vars)
			if err != nil {
				t.Fatalf("refresh: %v", err)
			}
			if res.AccessToken != "new" || res.RefreshToken != "rt2" {
				t.Fatalf("result = %+v", res)
			}
			if gotPath != connectorRefreshPath {
				t.Fatalf("path = %q, want %q", gotPath, connectorRefreshPath)
			}
			if gotBody["connector"] != "demo" || gotBody["refresh_token"] != "rt1" {
				t.Fatalf("body = %v", gotBody)
			}
			gotVars, present := gotBody["vars"]
			if tc.wantVars == nil {
				if present {
					t.Fatalf("vars must be omitted, got %v", gotVars)
				}
				return
			}
			if !reflect.DeepEqual(gotVars, any(tc.wantVars)) {
				t.Fatalf("vars = %v, want %v", gotVars, tc.wantVars)
			}
		})
	}
}

// TestRefreshCarriesAndPreservesVars follows one connector through the loop's
// building blocks: the stored entry yields the vars, and the re-Write after a
// refresh keeps the stored credentials (including the vars) intact.
func TestRefreshCarriesAndPreservesVars(t *testing.T) {
	dir := t.TempDir()
	w := newConnectorWriter(dir, &fakeMCPGateway{}, nil)
	now := time.Now()

	creds := ConnectorCreds{
		Connector:    "demo",
		AuthType:     "oauth",
		AccessToken:  "at1",
		RefreshToken: "rt1",
		ExpiresAt:    now.Add(time.Minute).Unix(),
		Refresh:      true,
		Credentials: map[string]string{
			"shop":                  "a.myshopify.com",
			credentialConnectorVars: `{"shop":"a.myshopify.com"}`,
		},
	}
	if err := w.Write(context.Background(), creds); err != nil {
		t.Fatalf("Write: %v", err)
	}

	targets := w.RefreshableEntries()
	if len(targets) != 1 {
		t.Fatalf("targets = %+v, want 1", targets)
	}
	if want := map[string]string{"shop": "a.myshopify.com"}; !reflect.DeepEqual(targets[0].Vars, want) {
		t.Fatalf("target vars = %#v, want %#v", targets[0].Vars, want)
	}

	h := &DeviceMQTTHandler{}
	refreshed, ok, err := h.loadRefreshedCreds(w, "demo", connectorRefreshResult{AccessToken: "at2", RefreshToken: "rt2", ExpiresIn: 3600}, now)
	if err != nil || !ok {
		t.Fatalf("loadRefreshedCreds ok=%v err=%v", ok, err)
	}
	if refreshed.AccessToken != "at2" || refreshed.RefreshToken != "rt2" {
		t.Fatalf("tokens not merged: %+v", refreshed)
	}
	if err := w.Write(context.Background(), refreshed); err != nil {
		t.Fatalf("re-Write: %v", err)
	}

	stored, ok, err := w.loadEntry("demo")
	if err != nil || !ok {
		t.Fatalf("loadEntry ok=%v err=%v", ok, err)
	}
	if stored.Credentials[credentialConnectorVars] != `{"shop":"a.myshopify.com"}` || stored.Credentials["shop"] != "a.myshopify.com" {
		t.Fatalf("credentials lost across refresh: %#v", stored.Credentials)
	}
	if next := w.RefreshableEntries(); len(next) != 1 || next[0].Vars["shop"] != "a.myshopify.com" {
		t.Fatalf("vars lost for the next refresh: %+v", next)
	}
}

// TestClientCredentialConnectorStaysOutOfTheRefreshLoop covers a connector linked
// with the customer's own app (a client ID and secret, no refresh token): the
// entry is stored as a PAT-style entry carrying a short-lived access token, and
// it never appears in the refresh loop, because the loop only picks entries with a
// refresh_token AND refresh:true. The skill helper renews such a token itself.
func TestClientCredentialConnectorStaysOutOfTheRefreshLoop(t *testing.T) {
	dir := t.TempDir()
	w := newConnectorWriter(dir, &fakeMCPGateway{}, nil)
	now := time.Now()

	// A 24 hour token with no refresh token must keep its expiry (it is not the
	// "static secret" carve-out, which only applies when there is no access token).
	expiresAt := resolveConnectorExpiresAt("pat", "minted-token", "", 0, 86399, now)
	if want := now.Add(86399 * time.Second).Unix(); expiresAt != want {
		t.Fatalf("expires_at = %d, want %d", expiresAt, want)
	}

	creds := ConnectorCreds{
		Connector:   "shopify",
		AuthType:    "pat",
		AccessToken: "minted-token",
		ExpiresAt:   expiresAt,
		Credentials: map[string]string{
			"shop":          "a.myshopify.com",
			"client_id":     "the-client-id",
			"client_secret": "the-client-secret",
		},
		Refresh: false,
	}
	if err := w.Write(context.Background(), creds); err != nil {
		t.Fatalf("Write: %v", err)
	}

	if got := w.RefreshableEntries(); len(got) != 0 {
		t.Fatalf("a client-credentials entry must not be refreshed by the loop, got %+v", got)
	}

	// The on-disk shape the skill helper reads.
	raw, err := os.ReadFile(filepath.Join(dir, "shopify_access_tokens.json"))
	if err != nil {
		t.Fatalf("read entry file: %v", err)
	}
	var file struct {
		Version    int                       `json:"version"`
		Connectors map[string]map[string]any `json:"connectors"`
	}
	if err := json.Unmarshal(raw, &file); err != nil {
		t.Fatalf("parse entry file: %v", err)
	}
	entry := file.Connectors["shopify"]
	if file.Version != 1 || entry["auth_type"] != "pat" || entry["access_token"] != "minted-token" {
		t.Fatalf("unexpected entry: %s", raw)
	}
	if int64(entry["expires_at"].(float64)) != expiresAt {
		t.Fatalf("expires_at not stored: %s", raw)
	}
	for _, key := range []string{"refresh_token", "refresh"} {
		if _, present := entry[key]; present {
			t.Fatalf("entry must not carry %q: %s", key, raw)
		}
	}
	stored, _ := entry["credentials"].(map[string]any)
	if stored["shop"] != "a.myshopify.com" || stored["client_id"] != "the-client-id" || stored["client_secret"] != "the-client-secret" {
		t.Fatalf("credentials not stored as sent: %s", raw)
	}
	if info, err := os.Stat(filepath.Join(dir, "shopify_access_tokens.json")); err != nil || info.Mode().Perm() != 0o600 {
		t.Fatalf("entry file mode = %v err = %v, want 0600", info, err)
	}
}
