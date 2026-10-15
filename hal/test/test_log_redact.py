"""Tests for hal.log_redact — user data stays out of shipped GELF records while
local logs keep every record in full."""

import io
import json
import logging
import sys

import pytest

from hal.drivers.gelf_handler import GELFHandler
from hal.log_redact import gelf_message, private


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record)


@pytest.fixture
def log():
    """A logger whose records come back as (local text, GELF text)."""
    capture = _Capture()
    logger = logging.getLogger("hal.test.log_redact")
    logger.propagate = False
    logger.setLevel(logging.DEBUG)
    logger.addHandler(capture)

    def shipped():
        out = [(r.getMessage(), gelf_message(r)) for r in capture.records]
        capture.records.clear()
        return out

    logger.shipped = shipped
    yield logger
    logger.removeHandler(capture)


def test_private_formats_like_the_raw_value_locally():
    assert "said: %s" % private("turn on the light") == "said: turn on the light"
    assert "said: %r" % private("hi") == "said: 'hi'"
    assert f"{private('hi'):>4}" == "  hi"
    assert private(private("x")).value == "x"  # wrapping twice is harmless


def test_percent_templates_ship_with_values_redacted_by_default(log):
    text, model, speaker = "call my mom", "nova-3", "Alice"
    log.info("STT final (provider=%s, %d ms): %r", "deepgram", 120, text)
    log.info("Service %s started", "tts")
    log.info("heard from %s: %r", speaker, "hello")
    log.info("Using %s", model)  # no key in the template: the argument's name counts
    log.info("status: %s", text)  # the argument's name wins over the template's key
    log.info("model=%s", private("nova-3"))
    assert log.shipped() == [
        ("STT final (provider=deepgram, 120 ms): 'call my mom'",
         "STT final (provider=deepgram, 120 ms): <redacted 11 chars>"),
        ("Service tts started", "Service <redacted 3 chars> started"),
        ("heard from Alice: 'hello'", "heard from <redacted 5 chars>: <redacted 5 chars>"),
        ("Using nova-3", "Using nova-3"),
        ("status: call my mom", "status: <redacted 11 chars>"),
        ("model=nova-3", "model=<redacted 6 chars>"),
    ]


def test_numbers_and_errors_ship(log):
    log.info("latency %d ms (%.1f%%)", 42, 12.34)
    log.error("Camera failed: %s", RuntimeError("no /dev/video0"))
    assert [g for _, g in log.shipped()] == [
        "latency 42 ms (12.3%)",
        "Camera failed: no /dev/video0",
    ]


def test_f_strings_ship_their_literal_text_only(log):
    transcript, provider, latency, frames = "my pin is 1234", "deepgram", 12.345, 7
    phone = "0912345678"
    try:
        raise TimeoutError("read timed out")
    except TimeoutError as e:
        log.warning(f"STT via {provider} failed after {latency:.1f}s: {e}")
    log.info(f"STT final segment: {transcript!r} ({frames} frames)")
    log.info(f"dialing {phone}")
    log.info(
        "[voice] heard " + transcript
    )
    assert log.shipped() == [
        ("STT via deepgram failed after 12.3s: read timed out",
         "STT via deepgram failed after 12.3s: read timed out"),
        ("STT final segment: 'my pin is 1234' (7 frames)",
         "STT final segment: <redacted 16 chars> (7 frames)"),
        ("dialing 0912345678", "dialing <redacted 10 chars>"),
        ("[voice] heard my pin is 1234", "[voice] heard <redacted 14 chars>"),
    ]


def test_ambiguous_f_string_redacts_every_value(log):
    # The value repeats the template's literal text, so it is unclear where the
    # safe value starts: nothing ships.
    text, provider = "x provider=Alice", "deepgram"
    log.info(f"{text} provider={provider}")
    assert log.shipped() == [
        ("x provider=Alice provider=deepgram", "<redacted 1 chars> provider=<redacted 23 chars>"),
    ]


def test_messages_not_written_as_literals_are_redacted(log):
    msg = f"camera {'Alice'} left"
    log.info(msg)
    log.info({"user": "alice"})
    log.error(ValueError("bad frame"))
    assert [g for _, g in log.shipped()] == [
        "<redacted 17 chars>",
        "<redacted>",
        "bad frame",
    ]


def test_dict_args_outside_safe_keys_are_redacted(log):
    log.info("%(who)s via %(provider)s", {"who": "alice", "provider": "deepgram"})
    assert [g for _, g in log.shipped()] == ["<redacted 5 chars> via deepgram"]


def _record_here(msg, *args):
    """A record whose call site is the caller's line, built without logging it
    (pytest's own log handler raises on a template that does not fit)."""
    line = sys._getframe(1).f_lineno
    return logging.LogRecord("hal", logging.INFO, __file__, line, msg, args, None)


def test_a_template_that_does_not_fit_drops_its_arguments():
    record = _record_here("count=%d", "Alice")
    assert gelf_message(record) == "count=%d <arguments redacted>"


def test_a_record_without_a_call_site_is_redacted():
    record = logging.LogRecord("hal", logging.INFO, "/nonexistent.py", 1, "hello %s", ("Alice",), None)
    assert gelf_message(record) == "<redacted 8 chars>"


def test_gelf_handler_ships_redacted_text_while_the_local_log_keeps_it(monkeypatch, tmp_path):
    monkeypatch.delenv("GELF_URL", raising=False)
    # No device key yet: the GELF record lands in the spool synchronously.
    gelf = GELFHandler(os_cfg_get=lambda key, default="": default, spool_dir=str(tmp_path), start_worker=False)
    gelf.setFormatter(logging.Formatter("%(message)s"))
    local_out = io.StringIO()
    local = logging.StreamHandler(local_out)
    local.setFormatter(logging.Formatter("%(message)s"))

    logger = logging.getLogger("hal.test.log_redact.handler")
    logger.propagate = False
    logger.setLevel(logging.INFO)
    logger.addHandler(local)
    logger.addHandler(gelf)
    text = "my pin is 1234"
    try:
        logger.info(f"STT final segment: {text!r}")
    finally:
        logger.removeHandler(local)
        logger.removeHandler(gelf)

    assert local_out.getvalue() == "STT final segment: 'my pin is 1234'\n"
    shipped = [json.loads(line) for line in gelf._spool.take()]
    assert [m["short_message"] for m in shipped] == ["STT final segment: <redacted 16 chars>"]


def test_gelf_handler_scrubs_tokens_and_url_queries(monkeypatch, tmp_path):
    monkeypatch.delenv("GELF_URL", raising=False)
    gelf = GELFHandler(os_cfg_get=lambda key, default="": default, spool_dir=str(tmp_path), start_worker=False)
    gelf.setFormatter(logging.Formatter("%(message)s"))
    logger = logging.getLogger("hal.test.log_redact.token")
    logger.propagate = False
    logger.setLevel(logging.INFO)
    logger.addHandler(gelf)
    token = "123456789:AAH4fakeFAKEfake_fake-FAKEfakeFAKEfak"
    try:
        logger.warning("telegram send failed: %s",
                       ConnectionError(f"HTTPSConnectionPool: /bot{token}/sendMessage (timeout)"))
        try:
            raise ConnectionError("Max retries exceeded with url: /user/info?name=Alice")
        except ConnectionError:
            logger.exception("user lookup failed")
    finally:
        logger.removeHandler(gelf)

    shipped = [json.loads(line)["short_message"] for line in gelf._spool.take()]
    assert shipped[0] == "telegram send failed: HTTPSConnectionPool: /bot<redacted-token>/sendMessage (timeout)"
    assert shipped[1].startswith("user lookup failed\nTraceback")
    assert "Alice" not in shipped[1] and "/user/info?<redacted>" in shipped[1]
