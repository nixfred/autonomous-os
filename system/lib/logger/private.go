package logger

import (
	"fmt"
	"log/slog"
	"regexp"
	"strings"
)

// telegramBotToken matches a Telegram bot token (<bot id>:<secret>). Errors
// from Bot API calls quote the request URL, which embeds it.
var telegramBotToken = regexp.MustCompile(`\d{6,12}:[A-Za-z0-9_-]{30,}`)

// urlQuery matches the query string of a URL ("?name=Alice&q=..."): request
// errors quote the URL.
var urlQuery = regexp.MustCompile(`\?[^\s#'"]*=[^\s#'"]*`)

// scrubSecrets removes credentials and URL query strings that can ride along in
// error text before a record leaves the device.
func scrubSecrets(s string) string {
	s = telegramBotToken.ReplaceAllString(s, "<redacted-token>")
	return urlQuery.ReplaceAllString(s, "?<redacted>")
}

// privateValue is a log attribute value that may be written locally but never
// shipped off the device.
type privateValue struct{ v any }

// String renders the raw value for the local console and file handlers.
func (p privateValue) String() string { return fmt.Sprint(p.v) }

// Private redacts a log attribute value in GELF even under a key in
// gelfSafeKeys. Strings under other keys are redacted anyway (default-deny), so
// Private is needed only where a safe key can carry user data.
//
//	slog.Info("[voice] turn rejected", "reason", logger.Private(reason))
//
// The console and the log file show the value as usual; the GELF handler ships
// "<redacted N chars>" in its place.
func Private(v any) any {
	if p, ok := v.(privateValue); ok {
		return p
	}
	return privateValue{v: v}
}

// gelfSafeKeys are the attribute keys whose string values may ship to GELF:
// technical ids, enumerations, versions and device configuration. GELF is
// default-deny: a string under any other key ships as "<redacted N chars>", so
// a new log line can never leak user data by forgetting Private. Numbers,
// booleans, durations, times and errors always ship. Never add a key that can
// hold what a person said, who they are or what the device observed about them.
var gelfSafeKeys = map[string]bool{
	// errors
	"error": true, "err": true, "panic": true, "stack": true,
	// component and run/turn/event ids
	"component": true, "run_id": true, "runID": true, "runId": true, "reqId": true,
	"req_id": true, "request_id": true, "event_id": true, "event_name": true,
	"turn_id": true, "updateId": true, "itemId": true, "responseID": true,
	"openclaw_run_id": true, "device_run_id": true, "flow_run_id": true,
	"flowRunId": true, "emitRunID": true, "active_run_id": true, "current_run": true,
	"openclawId": true, "schedule_id": true, "job_id": true, "intent_id": true,
	"question_id": true, "result_id": true, "attempt_id": true, "toolCallId": true,
	"agent_id": true, "main_agent_id": true, "selected_agent_id": true,
	"deviceId": true, "device_type": true, "mac": true, "text_key": true, "sha8": true,
	// enumerations and states
	"type": true, "kind": true, "status": true, "reason": true,
	"reasons": true, "outcome": true, "mode": true, "phase": true, "state": true,
	"step": true, "op": true, "role": true, "chatType": true, "code": true,
	"level": true, "source": true, "owner": true, "pool": true,
	"section": true, "types": true, "auth_type": true, "intent": true,
	"sensing_type": true, "setup_failure_reason": true, "sentinel": true,
	"filler": true, "phrase": true, "direction": true, "via": true, "route": true,
	"probe": true, "param_keys": true, "config_keys": true,
	// runtimes, providers, skills and hardware
	"runtime": true, "agent_runtime": true, "backend": true, "provider": true,
	"model": true, "os_model": true, "llm_model": true, "operator_model": true,
	"upstream_default": true, "tts_provider": true, "tts_voice": true,
	"skill": true, "skills": true, "connector": true, "tool": true, "hook": true,
	"capability": true, "plugin": true, "package": true,
	"topic": true, "unit": true, "servo": true, "led": true, "color": true,
	"gateway.protocol": true, "gateway.default": true,
	// versions
	"version": true, "os": true, "hal_version": true, "codex_version": true,
	"opencode_version": true, "picoclaw_version": true, "claudecode_version": true,
	"hermes_version": true, "openclaw_version": true,
	// device network and locale configuration
	"base_url": true, "llm_base_url": true, "broker": true, "metadataURL": true,
	"endpoint": true, "ssid": true, "timezone": true, "tz": true, "language": true,
	"lang": true, "stt_language": true, "lan_ip": true, "local_ip": true, "ip": true,
	"interface": true, "network": true, "wpa_state": true,
}

// gelfAttrValue renders an attribute for GELF under the default-deny policy:
// Private values and strings under keys outside gelfSafeKeys are redacted, and
// credentials are scrubbed from whatever ships.
func gelfAttrValue(key string, v slog.Value) string {
	v = v.Resolve()
	switch v.Kind() {
	case slog.KindInt64, slog.KindUint64, slog.KindFloat64, slog.KindBool,
		slog.KindDuration, slog.KindTime:
		return v.String()
	case slog.KindString:
		if gelfSafeKeys[key] {
			return scrubSecrets(v.String())
		}
		return redactedText(v.String())
	case slog.KindAny:
		switch raw := v.Any().(type) {
		case privateValue:
			return redactedValue(raw.v)
		case error:
			return scrubSecrets(raw.Error())
		}
		if gelfSafeKeys[key] {
			return scrubSecrets(v.String())
		}
		return redactedValue(v.Any())
	default: // groups
		return "<redacted>"
	}
}

func redactedText(s string) string {
	return fmt.Sprintf("<redacted %d chars>", len([]rune(s)))
}

func redactedValue(v any) string {
	switch raw := v.(type) {
	case string:
		return redactedText(raw)
	case []byte:
		return fmt.Sprintf("<redacted %d bytes>", len(raw))
	default:
		return "<redacted>"
	}
}

// stdLogTag matches the "[component]" prefix of a std log line.
var stdLogTag = regexp.MustCompile(`^\s*\[[^\]]{1,40}\]`)

// gelfMessage renders a record's message for GELF. slog messages are constant
// strings (user data goes in attributes) and ship as is. A line from the std
// log package arrives already formatted, with no caller (PC 0), so only its
// "[component]" tag ships.
func gelfMessage(r slog.Record) string {
	if r.PC != 0 {
		return scrubSecrets(r.Message)
	}
	tag := stdLogTag.FindString(r.Message)
	return strings.TrimSpace(tag + " " + redactedText(strings.TrimSpace(r.Message[len(tag):])))
}
