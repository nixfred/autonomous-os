"""Language gate: only speech probed as the selected language opens a live session."""

import struct
import types

import numpy as np

from hal.drivers.voice._internal import config as voice_cfg
from hal.drivers.voice.lang_id import LanguageGuess
from hal.drivers.voice.lang_id import gate as gate_mod
from hal.drivers.voice.lang_id.gate import LanguageGate, build_gate, resolve_language
from hal.drivers.voice.voice_service import VoiceService

FRAME = 1024  # 64 ms at 16 kHz
SPEECH = struct.pack("<h", 3000) * FRAME
SILENCE = b"\x00\x00" * FRAME


class FakeIdentifier:
    languages = ["en"]

    def __init__(self, language="en", probability=1.0, in_set=0.9):
        self.guess = (language, probability, in_set)
        self.calls = []

    def identify(self, pcm):
        self.calls.append(len(pcm) / 32000)
        language, probability, in_set = self.guess
        return LanguageGuess(language, probability, len(pcm) / 32000, in_set)


def make_gate(identifier, **kwargs):
    options = dict(mode="enforce", min_prob=0.5, speech_s=1.0, max_wait_s=2.5,
                   speech_rms=300, utterance_gap_s=1.0, min_speech_s=0.7)
    options.update(kwargs)
    return LanguageGate(identifier, **options)


def test_selected_language():
    assert resolve_language("ja") == "ja"
    assert resolve_language("") is None  # web "Auto"
    assert resolve_language(None) is None
    assert resolve_language("xx") is None


def test_no_gate_without_a_selected_language():
    assert build_gate("") is None
    assert build_gate(None) is None


def test_no_gate_without_the_model(monkeypatch, tmp_path):
    monkeypatch.setattr(voice_cfg, "LANG_ID_MODEL_PATH", tmp_path / "missing.onnx")
    monkeypatch.setattr(voice_cfg, "LANG_ID_MODEL_MANIFEST", tmp_path / "missing.json")
    assert build_gate("en") is None


def test_no_gate_when_off(monkeypatch):
    monkeypatch.setattr(voice_cfg, "LANG_GATE_MODE", gate_mod.MODE_OFF)
    assert build_gate("en") is None


def test_identification_error_fails_open():
    class Broken:
        def identify(self, pcm):
            raise RuntimeError("onnx")

    decision = make_gate(Broken()).decide(SPEECH * 16, 1.0)
    assert decision.accept and decision.reason == "unavailable"


def test_accepts_configured_language():
    decision = make_gate(FakeIdentifier(in_set=0.8)).decide(SPEECH * 16, 1.0)
    assert decision.accept and decision.reason == "in_set"


def test_rejects_other_language():
    decision = make_gate(FakeIdentifier(in_set=0.1)).decide(SPEECH * 16, 1.0)
    assert not decision.accept and decision.reason == "out_of_set"


def test_short_capture_follows_policy():
    identifier = FakeIdentifier()
    assert not make_gate(identifier).decide(SPEECH * 4, 0.25).accept
    assert make_gate(identifier, short_policy="accept").decide(SPEECH * 4, 0.25).accept
    assert identifier.calls == []  # too short to probe


def test_utterance_between_minimum_and_target_is_judged():
    identifier = FakeIdentifier(in_set=0.9)
    decision = make_gate(identifier).decide(SPEECH * 13, 0.83)
    assert decision.accept and decision.reason == "in_set"
    assert identifier.calls == [13 * FRAME * 2 / 32000]
    assert not make_gate(FakeIdentifier(in_set=0.1)).decide(SPEECH * 13, 0.83).accept


def test_below_minimum_is_too_short():
    identifier = FakeIdentifier()
    decision = make_gate(identifier).decide(SPEECH * 10, 0.64)
    assert not decision.accept and decision.reason == "too_short"
    assert identifier.calls == []


def test_shadow_never_rejects():
    gate = make_gate(FakeIdentifier(in_set=0.0), mode="shadow")
    decision = gate.decide(SPEECH * 16, 1.0)
    assert decision.accept and decision.reason == "shadow_reject"


class FakeMic:
    def __init__(self, frames):
        self.frames = list(frames)
        self.reads = 0

    def read(self, n):
        # Real capture backends return int16 arrays shaped (frames, 1).
        self.reads += 1
        data = self.frames.pop(0) if self.frames else SILENCE
        return np.frombuffer(data, dtype=np.int16).reshape(-1, 1), False


def fake_service(gate):
    service = object.__new__(VoiceService)
    service._np = np
    service._running = True
    service._language_gate = gate
    service._tts_is_speaking = lambda: False
    service._music_is_playing = lambda: False
    return service


def test_helper_collects_speech_into_pre_buffer_then_accepts(monkeypatch):
    monkeypatch.setattr(voice_cfg, "STT_RATE", 16000)
    identifier = FakeIdentifier(in_set=0.9)
    service = fake_service(make_gate(identifier))
    pre = [SPEECH] * 3
    mic = FakeMic([SPEECH] * 30)
    assert VoiceService._language_gate_accepts(service, mic, FRAME, 16000, pre)
    assert identifier.calls and identifier.calls[0] >= 1.0
    # Everything read while holding (and while classifying) stays in the pre-roll.
    assert len(pre) == 3 + mic.reads


def test_helper_rejects_out_of_set():
    service = fake_service(make_gate(FakeIdentifier(language="en", in_set=0.05)))
    assert not VoiceService._language_gate_accepts(
        service, FakeMic([SPEECH] * 30), FRAME, 16000, [SPEECH] * 3,
    )


def test_helper_stops_holding_at_utterance_end():
    identifier = FakeIdentifier()
    service = fake_service(make_gate(identifier))
    mic = FakeMic([SPEECH] * 2 + [SILENCE] * 40)
    assert not VoiceService._language_gate_accepts(service, mic, FRAME, 16000, [SPEECH] * 3)
    assert identifier.calls == []  # 0.3 s of speech: rejected as too short, no inference
    assert mic.reads < 30


def test_helper_judges_a_short_utterance_above_minimum():
    identifier = FakeIdentifier(in_set=0.9)
    service = fake_service(make_gate(identifier))
    mic = FakeMic([SPEECH] * 9 + [SILENCE] * 40)  # 3 + 9 frames = 0.77 s of speech
    assert VoiceService._language_gate_accepts(service, mic, FRAME, 16000, [SPEECH] * 3)
    assert identifier.calls and 0.7 <= identifier.calls[0] < 1.0


def test_no_gate_accepts_everything():
    service = fake_service(None)
    assert VoiceService._language_gate_accepts(service, FakeMic([]), FRAME, 16000, [])


def test_dump_writes_scored_speech_and_capture(tmp_path, monkeypatch):
    import wave

    monkeypatch.setattr(voice_cfg, "LANG_GATE_DUMP_DIR", str(tmp_path))
    service = fake_service(make_gate(FakeIdentifier(in_set=0.9)))
    assert VoiceService._language_gate_accepts(
        service, FakeMic([SPEECH] * 40), FRAME, 16000, [SPEECH] * 3,
    )
    files = sorted(p.name for p in tmp_path.iterdir())
    assert len(files) == 2 and all("_accept_in_set_in0.90_" in f for f in files)
    with wave.open(str(tmp_path / files[1])) as w:  # *_speech.wav
        assert w.getframerate() == 16000 and w.getnframes() >= 16000


def live_service(gate_accepts):
    service = object.__new__(VoiceService)
    service._music_is_playing = lambda: False
    service._hardware_aec_live_entry = lambda: True  # skip the noise classifier
    service._realtime = types.SimpleNamespace(
        prepared=0, wait_until_available=lambda timeout: True,
    )
    service._realtime.prepare_turn = lambda: setattr(
        service._realtime, "prepared", service._realtime.prepared + 1,
    )
    return service, (lambda: gate_accepts)


def test_live_decision_skips_without_preparing_on_reject(monkeypatch):
    from hal import config as hal_config

    monkeypatch.setattr(hal_config, "REALTIME_ENABLED", True)
    monkeypatch.setattr(hal_config, "WAKEWORD_ENABLED", False)
    service, admit = live_service(False)
    assert VoiceService._live_decision(service, [], admit=admit) == "skip"
    assert service._realtime.prepared == 0


def test_live_decision_opens_on_accept(monkeypatch):
    from hal import config as hal_config

    monkeypatch.setattr(hal_config, "REALTIME_ENABLED", True)
    monkeypatch.setattr(hal_config, "WAKEWORD_ENABLED", False)
    service, admit = live_service(True)
    assert VoiceService._live_decision(service, [], admit=admit) == "live"
    assert service._realtime.prepared == 1
