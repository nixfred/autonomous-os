"""Pinned language-ID model: background download, verification, and the gate while it is not ready."""

import hashlib
import json
import struct
import threading

import numpy as np

from hal.drivers.voice._internal import config as voice_cfg
from hal.drivers.voice.lang_id import LanguageGuess
from hal.drivers.voice.lang_id import gate as gate_mod
from hal.drivers.voice.lang_id import model_store
from hal.drivers.voice.lang_id.gate import LanguageGate, build_gate
from hal.drivers.voice.lang_id.model_store import ModelStore, read_manifest
from hal.drivers.voice.voice_service import VoiceService

MODEL = b"onnx-bytes" * 1000
SHA = hashlib.sha256(MODEL).hexdigest()
FRAME = 1024
SPEECH = struct.pack("<h", 3000) * FRAME


def writer(payload):
    def fetch(url, dest, stop):
        dest.write_bytes(payload)
    return fetch


def wait_ready(store_ready, timeout=5.0):
    assert store_ready.wait(timeout), "model never became ready"


def test_manifest_requires_https_and_sha(tmp_path):
    good = tmp_path / "m.json"
    good.write_text(json.dumps({"url": "https://x/y.onnx", "sha256": SHA, "size": 5}))
    assert read_manifest(good) == {"url": "https://x/y.onnx", "sha256": SHA, "size": 5}
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"url": "http://x/y.onnx", "sha256": SHA}))
    assert read_manifest(bad) is None
    bad.write_text(json.dumps({"url": "https://x/y.onnx", "sha256": "abc"}))
    assert read_manifest(bad) is None
    assert read_manifest(tmp_path / "missing.json") is None


def test_cached_model_is_used_without_download(tmp_path):
    calls = []
    store = ModelStore(tmp_path, "ambernet.onnx", "https://x", SHA,
                       fetch=lambda *a: calls.append(a))
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_bytes(MODEL)
    ready = threading.Event()
    store.ensure(lambda path: ready.set())
    wait_ready(ready)
    assert calls == []


def test_download_is_verified_then_renamed(tmp_path):
    store = ModelStore(tmp_path, "ambernet.onnx", "https://x", SHA, fetch=writer(MODEL))
    got = []
    ready = threading.Event()
    store.ensure(lambda path: (got.append(path), ready.set()))
    wait_ready(ready)
    assert got == [store.path] and store.path.read_bytes() == MODEL
    assert store.path.name == f"ambernet-{SHA[:12]}.onnx"
    assert not list(tmp_path.glob("*.part"))


def test_hash_mismatch_is_discarded_and_retried(tmp_path):
    payloads = iter([b"truncated", MODEL])
    store = ModelStore(tmp_path, "ambernet.onnx", "https://x", SHA, retry_s=0.01,
                       fetch=lambda url, dest, stop: dest.write_bytes(next(payloads)))
    ready = threading.Event()
    store.ensure(lambda path: ready.set())
    wait_ready(ready)
    assert store.path.read_bytes() == MODEL
    assert not list(tmp_path.glob("*.part"))


def test_corrupt_cached_file_is_replaced(tmp_path):
    store = ModelStore(tmp_path, "ambernet.onnx", "https://x", SHA, fetch=writer(MODEL))
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_bytes(MODEL[: len(MODEL) // 2])  # e.g. a copy cut short
    ready = threading.Event()
    store.ensure(lambda path: ready.set())
    wait_ready(ready)
    assert store.path.read_bytes() == MODEL


def test_interrupted_download_never_reaches_the_final_path(tmp_path):
    def fails_midway(url, dest, stop):
        dest.write_bytes(MODEL[:100])
        raise ConnectionError("reset")

    store = ModelStore(tmp_path, "ambernet.onnx", "https://x", SHA, retry_s=60,
                       fetch=fails_midway)
    store.ensure(lambda path: None)
    store._thread.join(timeout=0.5)
    assert not store.path.exists() and not list(tmp_path.glob("*.part"))
    store.stop()


class FakeIdentifier:
    languages = ["en"]
    usable = True

    def __init__(self, loads=True, in_set=0.0):
        self.loads = loads
        self.in_set = in_set

    def warm_up(self):
        return self.loads

    def identify(self, pcm):
        return LanguageGuess("en", 1.0, len(pcm) / 32000, self.in_set)


def make_gate():
    return LanguageGate(None, mode="enforce", min_prob=0.5, speech_s=1.0, max_wait_s=2.5,
                        speech_rms=300, utterance_gap_s=1.0, min_speech_s=0.7)


class CountingMic:
    def __init__(self):
        self.reads = 0

    def read(self, n):
        self.reads += 1
        return np.frombuffer(SPEECH, dtype=np.int16).reshape(-1, 1), False


def service_with(gate):
    service = object.__new__(VoiceService)
    service._np = np
    service._running = True
    service._language_gate = gate
    service._tts_is_speaking = lambda: False
    service._music_is_playing = lambda: False
    return service


def test_gate_passes_without_holding_while_the_model_downloads(tmp_path, monkeypatch):
    monkeypatch.setattr(voice_cfg, "STT_RATE", 16000)
    release = threading.Event()

    def slow_fetch(url, dest, stop):
        dest.write_bytes(MODEL[:100])  # partial file on disk mid-download
        release.wait(5)
        dest.write_bytes(MODEL)

    gate = make_gate()
    store = ModelStore(tmp_path, "ambernet.onnx", "https://x", SHA, fetch=slow_fetch)
    attached = threading.Event()
    store.ensure(lambda path: (gate.attach(FakeIdentifier(in_set=0.0)), attached.set()))

    # Mid-download: the gate is inactive, so out-of-set speech is NOT held or judged.
    mic = CountingMic()
    assert not gate.active
    assert VoiceService._language_gate_accepts(service_with(gate), mic, FRAME, 16000, [SPEECH] * 3)
    assert mic.reads == 0
    assert not store.path.exists()

    # Download completes: the gate turns active without a restart and rejects again.
    release.set()
    wait_ready(attached)
    assert gate.active
    mic = CountingMic()
    assert not VoiceService._language_gate_accepts(service_with(gate), mic, FRAME, 16000, [SPEECH] * 3)
    assert mic.reads > 0


def test_model_that_fails_to_load_leaves_the_gate_inactive():
    gate = make_gate()
    assert not gate.attach(FakeIdentifier(loads=False))
    mic = CountingMic()
    assert not gate.active
    assert VoiceService._language_gate_accepts(service_with(gate), mic, FRAME, 16000, [SPEECH] * 3)
    assert mic.reads == 0


def test_build_gate_downloads_when_no_local_model(tmp_path, monkeypatch):
    manifest = tmp_path / "ambernet.model.json"
    manifest.write_text(json.dumps({"url": "https://x/ambernet.onnx", "sha256": SHA}))
    release = threading.Event()
    fetched = threading.Event()

    def blocked_fetch(url, dest, stop):
        fetched.set()
        release.wait(5)

    monkeypatch.setattr(voice_cfg, "LANG_GATE_MODE", "enforce")
    monkeypatch.setattr(voice_cfg, "LANG_ID_MODEL_PATH", tmp_path / "missing" / "ambernet.onnx")
    monkeypatch.setattr(voice_cfg, "LANG_ID_MODEL_MANIFEST", manifest)
    monkeypatch.setattr(voice_cfg, "LANG_ID_CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(model_store, "_http_fetch", blocked_fetch)
    gate = build_gate("en")
    try:
        assert gate is not None and not gate.active
        assert fetched.wait(5)
    finally:
        gate.store.stop()
        release.set()


def test_build_gate_off_without_model_or_pin(tmp_path, monkeypatch):
    monkeypatch.setattr(voice_cfg, "LANG_GATE_MODE", gate_mod.MODE_ENFORCE)
    monkeypatch.setattr(voice_cfg, "LANG_ID_MODEL_PATH", tmp_path / "missing.onnx")
    monkeypatch.setattr(voice_cfg, "LANG_ID_MODEL_MANIFEST", tmp_path / "missing.json")
    assert build_gate("en") is None
