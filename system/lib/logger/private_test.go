package logger

import (
	"encoding/json"
	"errors"
	"log"
	"log/slog"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func TestPrivateAttrStaysLocalAndIsRedactedInGELF(t *testing.T) {
	collector := newGELFCollector(t)
	t.Setenv("GELF_URL", collector.URL)
	prev := slog.Default()
	logPath := filepath.Join(t.TempDir(), "os-server.log")
	cleanup := Init(logPath)

	slog.Info("[voice] final transcript", "text", Private("turn on the lamp"), "chars", 16)
	slog.With("reply", Private("Đã bật đèn")).Info("[voice] agent reply")
	cleanup()
	slog.SetDefault(prev)

	got := collector.received()
	if len(got) != 2 {
		t.Fatalf("GELF records = %d, want 2", len(got))
	}
	for _, r := range got {
		if strings.Contains(r.body, "turn on the lamp") || strings.Contains(r.body, "Đã bật đèn") {
			t.Errorf("GELF body leaks conversation content: %s", r.body)
		}
	}
	transcript, reply := decodeGELF(t, got[0].body), decodeGELF(t, got[1].body)
	if v := transcript["_text"]; v != "<redacted 16 chars>" {
		t.Errorf("_text = %v, want the transcript redacted with its length", v)
	}
	if v := transcript["_chars"]; v != "16" {
		t.Errorf("_chars = %v, want non-private attrs shipped unchanged", v)
	}
	if v := reply["_reply"]; v != "<redacted 10 chars>" {
		t.Errorf("_reply = %v, want a With attr redacted too", v)
	}

	local, err := os.ReadFile(logPath)
	if err != nil {
		t.Fatal(err)
	}
	for _, want := range []string{"turn on the lamp", "Đã bật đèn"} {
		if !strings.Contains(string(local), want) {
			t.Errorf("local log lacks %q; it should keep the content readable on the device:\n%s", want, local)
		}
	}
}

func TestPrivateIsIdempotent(t *testing.T) {
	once := Private("hello")
	if twice := Private(once); twice != once {
		t.Errorf("Private(Private(x)) = %#v, want %#v", twice, once)
	}
}

func decodeGELF(t *testing.T, body string) map[string]any {
	t.Helper()
	var m map[string]any
	if err := json.Unmarshal([]byte(body), &m); err != nil {
		t.Fatalf("decode GELF body %q: %v", body, err)
	}
	return m
}

func TestGELFScrubsTokensAndURLQueries(t *testing.T) {
	collector := newGELFCollector(t)
	t.Setenv("GELF_URL", collector.URL)
	prev := slog.Default()
	cleanup := Init(filepath.Join(t.TempDir(), "os-server.log"))

	const token = "123456789:AAH4fakeFAKEfake_fake-FAKEfakeFAKEfak"
	err := &url.Error{Op: "Post", URL: "https://api.telegram.org/bot" + token + "/sendMessage", Err: errors.New("i/o timeout")}
	slog.Error("telegram sendMessage failed", "err", err)
	slog.Error("telegram send failed: " + err.Error())
	lookup := &url.Error{Op: "Get", URL: "http://127.0.0.1:5001/user/info?name=Hieu", Err: errors.New("i/o timeout")}
	slog.Warn("user context: fetch failed", "error", lookup)
	cleanup()
	slog.SetDefault(prev)

	got := collector.received()
	if len(got) != 3 {
		t.Fatalf("GELF records = %d, want 3", len(got))
	}
	if v, _ := decodeGELF(t, got[2].body)["_error"].(string); strings.Contains(v, "Hieu") || !strings.Contains(v, "/user/info?<redacted>") {
		t.Errorf("_error = %q, want the URL query redacted", v)
	}
	for _, r := range got {
		if strings.Contains(r.body, token) || strings.Contains(r.body, "AAH4fake") {
			t.Errorf("GELF body leaks the bot token: %s", r.body)
		}
		if !strings.Contains(r.body, "i/o timeout") {
			t.Errorf("GELF body = %s, want the error cause kept", r.body)
		}
	}
}

func TestGELFIsDefaultDeny(t *testing.T) {
	collector := newGELFCollector(t)
	t.Setenv("GELF_URL", collector.URL)
	prev := slog.Default()
	logPath := filepath.Join(t.TempDir(), "os-server.log")
	cleanup := Init(logPath)

	slog.Info("[voice] turn",
		"component", "voice", // safe key: ships
		"speaker", "Hieu", // unknown key, never marked Private: redacted
		"chars", 42, "final", true, "took", 1500*time.Millisecond, // non-strings ship
		"error", errors.New("dial tcp: i/o timeout"), // errors ship
		"items", []string{"a", "b"}) // structured values under unknown keys: redacted
	log.Printf("[factory-reset] TRIGGER received — user-agent=%q", "curl/8.0 Hieu")
	cleanup()
	slog.SetDefault(prev)

	got := collector.received()
	if len(got) != 2 {
		t.Fatalf("GELF records = %d, want 2", len(got))
	}
	turn := decodeGELF(t, got[0].body)
	want := map[string]any{
		"short_message": "[voice] turn",
		"_component":    "voice",
		"_speaker":      "<redacted 4 chars>",
		"_chars":        "42",
		"_final":        "true",
		"_took":         "1.5s",
		"_error":        "dial tcp: i/o timeout",
		"_items":        "<redacted>",
	}
	for k, v := range want {
		if turn[k] != v {
			t.Errorf("%s = %v, want %v", k, turn[k], v)
		}
	}
	stdLine := decodeGELF(t, got[1].body)
	if msg, _ := stdLine["short_message"].(string); !strings.HasPrefix(msg, "[factory-reset] <redacted ") || strings.Contains(msg, "Hieu") {
		t.Errorf("std log short_message = %q, want only its tag shipped", msg)
	}

	local, err := os.ReadFile(logPath)
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(local), "Hieu") {
		t.Errorf("local log lacks the unredacted attribute:\n%s", local)
	}
}
