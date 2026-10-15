package mqtthandler

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"log/slog"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"time"

	"github.com/gin-gonic/gin"

	"go.autonomous.ai/os/system/server/serializers"
)

// The Shopify connector is linked with the customer's own Shopify app: the
// store domain, the app's client ID and its client secret. The device mints
// short-lived tokens itself from those (the connectors skill helper does it on
// demand), so this page only has to save the three values and check them once.
const (
	shopifyConnectorCode = "shopify"
	// shopifyTokenCacheFile is the helper-owned token cache next to the
	// connector entry; it is dropped whenever the credentials change or go away.
	shopifyTokenCacheFile = "shopify.token.json"

	shopifyMintTimeout     = 20 * time.Second
	shopifyMaxMintResponse = 64 << 10
	shopifyDefaultLifetime = 3600
	shopifyMinLifetime     = 60
	shopifyMaxLifetime     = 7 * 24 * 3600
	maxShopifyFieldLen     = 256

	shopifyRejectedMessage  = "Shopify rejected the client ID/secret or the app isn't installed on that store."
	shopifyUnreachableError = "Couldn't reach Shopify to check the credentials. Check the device's internet connection and try again."
	shopifyShopHint         = "Enter your store's .myshopify.com address, for example your-store.myshopify.com."
)

// shopifyShopRE is the only host shape a token request is ever sent to.
var shopifyShopRE = regexp.MustCompile(`^[a-z0-9][a-z0-9-]*\.myshopify\.com$`)

// shopifyHandleRE matches a bare store handle that normalizeShopifyShop completes.
var shopifyHandleRE = regexp.MustCompile(`^[a-z0-9][a-z0-9-]*$`)

var (
	errShopifyRejected    = errors.New("shopify rejected the credentials")
	errShopifyUnreachable = errors.New("shopify unreachable")
)

// shopifyConnectRequest is the body of PUT /api/device/connectors/shopify.
type shopifyConnectRequest struct {
	Shop         string `json:"shop"`
	ClientID     string `json:"client_id"`
	ClientSecret string `json:"client_secret"`
}

// shopifyInfoResponse is the read-side shape. It never carries the client
// secret or a token — only whether a secret is saved.
type shopifyInfoResponse struct {
	Connector  string   `json:"connector"`
	Connected  bool     `json:"connected"`
	Shop       string   `json:"shop,omitempty"`
	ClientID   string   `json:"client_id,omitempty"`
	HasSecret  bool     `json:"has_secret"`
	Scopes     []string `json:"scopes,omitempty"`
	ObtainedAt int64    `json:"obtained_at,omitempty"`
}

// shopifyToken is the useful part of a client-credentials token response.
type shopifyToken struct {
	AccessToken string
	ExpiresIn   int
	Scopes      []string
}

// normalizeShopifyShop turns what a person typed or pasted into the store's
// permanent domain: trims, lowercases, drops a scheme and any path, completes a
// bare handle. It errors unless the result is exactly <handle>.myshopify.com.
func normalizeShopifyShop(raw string) (string, error) {
	s := strings.ToLower(strings.TrimSpace(raw))
	s = strings.TrimPrefix(strings.TrimPrefix(s, "https://"), "http://")
	if i := strings.IndexAny(s, "/?#"); i >= 0 {
		s = s[:i]
	}
	s = strings.TrimSpace(s)
	if shopifyHandleRE.MatchString(s) {
		s += ".myshopify.com"
	}
	if !shopifyShopRE.MatchString(s) {
		return "", errors.New(shopifyShopHint)
	}
	return s, nil
}

// checkShopifyField validates a pasted client ID / secret: present, bounded and
// free of whitespace or control characters.
func checkShopifyField(label, v string) string {
	if v == "" {
		return label + " is required."
	}
	if len(v) > maxShopifyFieldLen || strings.ContainsFunc(v, func(r rune) bool { return r <= ' ' || r == 0x7f }) {
		return label + " looks wrong: copy it again without spaces."
	}
	return ""
}

// newShopifyHTTPClient builds the client used for the one validation request:
// short timeout and never follows a redirect, so the secret can only ever go to
// the store host it was addressed to.
func newShopifyHTTPClient() *http.Client {
	return &http.Client{
		Timeout: shopifyMintTimeout,
		CheckRedirect: func(*http.Request, []*http.Request) error {
			return http.ErrUseLastResponse
		},
	}
}

func (h *DeviceMQTTHandler) shopifyClient() *http.Client {
	if h.shopifyHTTP != nil {
		return h.shopifyHTTP
	}
	return newShopifyHTTPClient()
}

// mintShopifyToken requests a token with the client-credentials grant from the
// store's own host. Errors are fixed values: they never carry the secret, the
// client ID or the response body.
func mintShopifyToken(ctx context.Context, client *http.Client, shop, clientID, clientSecret string) (shopifyToken, error) {
	form := url.Values{}
	form.Set("grant_type", "client_credentials")
	form.Set("client_id", clientID)
	form.Set("client_secret", clientSecret)

	req, err := http.NewRequestWithContext(ctx, http.MethodPost, "https://"+shop+"/admin/oauth/access_token", strings.NewReader(form.Encode()))
	if err != nil {
		return shopifyToken{}, errShopifyUnreachable
	}
	req.Header.Set("Content-Type", "application/x-www-form-urlencoded")
	req.Header.Set("Accept", "application/json")

	resp, err := client.Do(req)
	if err != nil {
		return shopifyToken{}, errShopifyUnreachable
	}
	defer resp.Body.Close()
	body, err := io.ReadAll(io.LimitReader(resp.Body, shopifyMaxMintResponse))
	if err != nil {
		return shopifyToken{}, errShopifyUnreachable
	}
	switch {
	case resp.StatusCode == http.StatusBadRequest || resp.StatusCode == http.StatusUnauthorized || resp.StatusCode == http.StatusForbidden:
		return shopifyToken{}, errShopifyRejected
	case resp.StatusCode != http.StatusOK:
		return shopifyToken{}, errShopifyUnreachable
	}

	var out struct {
		AccessToken string `json:"access_token"`
		ExpiresIn   int    `json:"expires_in"`
		Scope       string `json:"scope"`
	}
	if err := json.Unmarshal(body, &out); err != nil || out.AccessToken == "" {
		return shopifyToken{}, errShopifyUnreachable
	}
	life := out.ExpiresIn
	if life <= 0 {
		life = shopifyDefaultLifetime
	}
	if life < shopifyMinLifetime {
		life = shopifyMinLifetime
	}
	if life > shopifyMaxLifetime {
		life = shopifyMaxLifetime
	}
	return shopifyToken{
		AccessToken: out.AccessToken,
		ExpiresIn:   life,
		Scopes:      strings.FieldsFunc(out.Scope, func(r rune) bool { return r == ',' || r == ' ' }),
	}, nil
}

// dropShopifyTokenCache removes the helper's cached token so a changed or
// removed connection never reuses an old one.
func (h *DeviceMQTTHandler) dropShopifyTokenCache() {
	if h.connectorWriter == nil {
		return
	}
	if err := os.Remove(filepath.Join(h.connectorWriter.dir, shopifyTokenCacheFile)); err != nil && !errors.Is(err, os.ErrNotExist) {
		slog.Warn("shopify: could not remove the token cache", "component", "mqtt", "error", err)
	}
}

func (h *DeviceMQTTHandler) publishShopifyResult(kind string, data map[string]any) {
	data["initiator"] = "device_local"
	if h.publishConnectorResult != nil {
		_ = h.publishConnectorResult(kind, "success", "", data)
		return
	}
	_ = h.publishDataResult(kind, "success", "", data)
}

// SetShopify handles PUT /api/device/connectors/shopify: checks the three
// values against Shopify once, saves them through the same writer a cloud-linked
// connection uses, and tells the cloud the connector is connected (status only;
// the secret is never published).
func (h *DeviceMQTTHandler) SetShopify(c *gin.Context) {
	var req shopifyConnectRequest
	if err := c.ShouldBindJSON(&req); err != nil {
		c.JSON(http.StatusBadRequest, serializers.ResponseError("invalid body"))
		return
	}
	shop, err := normalizeShopifyShop(req.Shop)
	if err != nil {
		c.JSON(http.StatusBadRequest, serializers.ResponseError(err.Error()))
		return
	}
	clientID := strings.TrimSpace(req.ClientID)
	clientSecret := strings.TrimSpace(req.ClientSecret)
	if msg := checkShopifyField("Client ID", clientID); msg != "" {
		c.JSON(http.StatusBadRequest, serializers.ResponseError(msg))
		return
	}
	if msg := checkShopifyField("Client secret", clientSecret); msg != "" {
		c.JSON(http.StatusBadRequest, serializers.ResponseError(msg))
		return
	}
	writer := h.connectorWriterFor(shopifyConnectorCode)
	if writer == nil || h.connectorWriter == nil {
		c.JSON(http.StatusServiceUnavailable, serializers.ResponseError("no writer available"))
		return
	}

	ctx, cancel := context.WithTimeout(c.Request.Context(), connectorHTTPTimeout)
	defer cancel()
	tok, err := mintShopifyToken(ctx, h.shopifyClient(), shop, clientID, clientSecret)
	switch {
	case errors.Is(err, errShopifyRejected):
		c.JSON(http.StatusBadRequest, serializers.ResponseError(shopifyRejectedMessage))
		return
	case err != nil:
		c.JSON(http.StatusBadGateway, serializers.ResponseError(shopifyUnreachableError))
		return
	}

	now := time.Now()
	creds := ConnectorCreds{
		Connector:   shopifyConnectorCode,
		AuthType:    "pat",
		AccessToken: tok.AccessToken,
		ExpiresAt:   now.Add(time.Duration(tok.ExpiresIn) * time.Second).Unix(),
		Scopes:      tok.Scopes,
		ClientID:    clientID,
		Credentials: map[string]string{"shop": shop, "client_id": clientID, "client_secret": clientSecret},
		// The helper mints the next token itself; there is no refresh token.
		Refresh:    false,
		ObtainedAt: now.Unix(),
	}
	if err := writer.Write(ctx, creds); err != nil {
		slog.Warn("shopify: could not save the connection", "component", "mqtt", "error", err)
		c.JSON(http.StatusInternalServerError, serializers.ResponseError("could not save the connection"))
		return
	}
	h.dropShopifyTokenCache()
	h.publishShopifyResult("connector.set."+shopifyConnectorCode, map[string]any{
		"connector": shopifyConnectorCode,
		"auth_type": "pat",
	})

	c.JSON(http.StatusOK, serializers.ResponseSuccess(shopifyInfoResponse{
		Connector:  shopifyConnectorCode,
		Connected:  true,
		Shop:       shop,
		ClientID:   clientID,
		HasSecret:  true,
		Scopes:     tok.Scopes,
		ObtainedAt: creds.ObtainedAt,
	}))
}

// GetShopify handles GET /api/device/connectors/shopify.
func (h *DeviceMQTTHandler) GetShopify(c *gin.Context) {
	empty := shopifyInfoResponse{Connector: shopifyConnectorCode}
	if h.connectorWriter == nil {
		c.JSON(http.StatusOK, serializers.ResponseSuccess(empty))
		return
	}
	creds, ok, err := h.connectorWriter.loadEntry(shopifyConnectorCode)
	if err != nil {
		c.JSON(http.StatusInternalServerError, serializers.ResponseError("could not read the connection"))
		return
	}
	if !ok {
		c.JSON(http.StatusOK, serializers.ResponseSuccess(empty))
		return
	}
	clientID := creds.Credentials["client_id"]
	if clientID == "" {
		clientID = creds.ClientID
	}
	c.JSON(http.StatusOK, serializers.ResponseSuccess(shopifyInfoResponse{
		Connector:  shopifyConnectorCode,
		Connected:  true,
		Shop:       creds.Credentials["shop"],
		ClientID:   clientID,
		HasSecret:  creds.Credentials["client_secret"] != "",
		Scopes:     creds.Scopes,
		ObtainedAt: creds.ObtainedAt,
	}))
}

// RemoveShopify handles DELETE /api/device/connectors/shopify.
func (h *DeviceMQTTHandler) RemoveShopify(c *gin.Context) {
	writer := h.connectorWriterFor(shopifyConnectorCode)
	if writer == nil {
		c.JSON(http.StatusServiceUnavailable, serializers.ResponseError("no writer available"))
		return
	}
	ctx, cancel := context.WithTimeout(c.Request.Context(), connectorHTTPTimeout)
	defer cancel()
	removed, err := writer.Remove(ctx, shopifyConnectorCode)
	if err != nil {
		slog.Warn("shopify: could not remove the connection", "component", "mqtt", "error", err)
		c.JSON(http.StatusInternalServerError, serializers.ResponseError("could not remove the connection"))
		return
	}
	h.dropShopifyTokenCache()
	h.publishShopifyResult("connector.remove."+shopifyConnectorCode, map[string]any{
		"connector": shopifyConnectorCode,
		"removed":   removed,
	})
	c.JSON(http.StatusOK, serializers.ResponseSuccess(map[string]any{
		"connector": shopifyConnectorCode,
		"removed":   removed,
	}))
}
