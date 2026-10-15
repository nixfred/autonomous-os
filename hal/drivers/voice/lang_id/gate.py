"""Language gate: open a live session only for speech in the selected language.

The selected language is config.json `stt_language`; an empty value (the web's
"Auto" option) selects nothing and leaves the gate off. A capture opens a live
session only when the selected language holds at least `min_prob` of AmberNet's
full 107-language softmax over its first `speech_s` of speech, or over all of it
when the utterance ends sooner. A capture with less than `min_speech_s` of speech
cannot be judged reliably; `short_policy` decides it (default reject).

The gate is active only once its model is loaded: a local `HAL_LANG_ID_MODEL_PATH`
file, else the release asset pinned in `ambernet.model.json`, downloaded in the
background (see model_store.py). Until then, or after the model fails to load,
captures pass without a hold (fail open).
"""

import logging
import threading
from dataclasses import dataclass
from typing import Optional

from hal.drivers.voice.lang_id.identifier import LanguageGuess, LanguageIdentifier

logger = logging.getLogger("hal.voice.lang_id")

MODE_OFF = "off"
MODE_SHADOW = "shadow"  # classify and log, never reject
MODE_ENFORCE = "enforce"
MODES = (MODE_OFF, MODE_SHADOW, MODE_ENFORCE)


@dataclass(frozen=True)
class GateDecision:
    accept: bool
    reason: str  # in_set | out_of_set | too_short | unavailable | shadow_reject
    guess: Optional[LanguageGuess]
    speech_s: float
    elapsed_ms: float


def resolve_language(selected: Optional[str]) -> Optional[str]:
    """The supported language selected by config.json `stt_language`, else None."""
    from hal.presets import SUPPORTED_LANGS, normalize_language

    if not selected:
        return None
    code = normalize_language(selected)
    return code if code in SUPPORTED_LANGS else None


class LanguageGate:
    def __init__(self, identifier: Optional[LanguageIdentifier], *, mode: str, min_prob: float,
                 speech_s: float, max_wait_s: float, speech_rms: float, utterance_gap_s: float,
                 short_policy: str = "reject", min_speech_s: float | None = None):
        self.identifier = identifier
        # Set by attach() once the model is loaded; a gate built with an identifier
        # (tests) is ready at once.
        self._ready = threading.Event()
        if identifier is not None:
            self._ready.set()
        self.mode = mode if mode in MODES else MODE_ENFORCE
        self.min_prob = min_prob
        self.speech_s = speech_s
        self.max_wait_s = max_wait_s
        self.speech_rms = speech_rms
        self.utterance_gap_s = utterance_gap_s
        self.short_policy = short_policy
        self.min_speech_s = speech_s if min_speech_s is None else min(min_speech_s, speech_s)

    @property
    def enabled(self) -> bool:
        return self.mode != MODE_OFF

    @property
    def active(self) -> bool:
        """Whether captures are held and judged: enabled with a loaded, working model."""
        identifier = self.identifier
        return (self.enabled and self._ready.is_set() and identifier is not None
                and getattr(identifier, "usable", True))

    def attach(self, identifier: LanguageIdentifier) -> bool:
        """Load the model and start judging captures; False leaves the gate inactive."""
        if not identifier.warm_up():
            logger.error("Language gate stays inactive: model failed to load")
            return False
        self.identifier = identifier
        self._ready.set()
        logger.info("Language gate active (%s)", self.mode)
        return True

    def decide(self, speech_pcm16: bytes, speech_s: float, elapsed_ms: float = 0.0) -> GateDecision:
        """Judge mono 16 kHz PCM16 speech frames (non-speech already removed)."""
        if speech_s < self.min_speech_s:
            accept = self.short_policy == "accept" or self.mode == MODE_SHADOW
            return GateDecision(accept, "too_short", None, speech_s, elapsed_ms)
        try:
            guess = self.identifier.identify(speech_pcm16)
        except Exception as e:
            logger.warning("Language gate: identification failed, accepting: %s", e)
            guess = None
        if guess is None:
            return GateDecision(True, "unavailable", None, speech_s, elapsed_ms)
        if guess.in_set >= self.min_prob:
            return GateDecision(True, "in_set", guess, speech_s, elapsed_ms)
        if self.mode == MODE_SHADOW:
            return GateDecision(True, "shadow_reject", guess, speech_s, elapsed_ms)
        return GateDecision(False, "out_of_set", guess, speech_s, elapsed_ms)


def build_gate(selected: Optional[str]) -> Optional[LanguageGate]:
    """The gate for config.json `stt_language`, or None when it does not apply.

    The returned gate turns active once its model is loaded; a download in flight
    leaves it inactive (fail open) rather than delaying HAL start.
    """
    from hal.drivers.voice._internal import config as voice_cfg
    from hal.drivers.voice.lang_id.model_store import ModelStore, read_manifest

    if voice_cfg.LANG_GATE_MODE == MODE_OFF:
        return None
    language = resolve_language(selected)
    if language is None:
        logger.info("Language gate off: no language selected (stt_language=%r)", selected)
        return None

    def identifier_for(model_path) -> LanguageIdentifier:
        return LanguageIdentifier(
            model_path, voice_cfg.LANG_ID_LABELS_PATH, [language],
            threads=voice_cfg.LANG_ID_THREADS, max_seconds=voice_cfg.LANG_GATE_SPEECH_S,
            cpus=voice_cfg.LANG_ID_CPUS,
        )

    local = voice_cfg.LANG_ID_MODEL_PATH
    manifest = None if local.is_file() else read_manifest(voice_cfg.LANG_ID_MODEL_MANIFEST)
    if not local.is_file() and manifest is None:
        logger.warning("Language gate off: no model at %s and no valid pin at %s",
                       local, voice_cfg.LANG_ID_MODEL_MANIFEST)
        return None
    gate = LanguageGate(
        None,
        mode=voice_cfg.LANG_GATE_MODE,
        min_prob=voice_cfg.LANG_GATE_MIN_PROB,
        speech_s=voice_cfg.LANG_GATE_SPEECH_S,
        max_wait_s=voice_cfg.LANG_GATE_MAX_WAIT_S,
        speech_rms=voice_cfg.LANG_GATE_SPEECH_RMS,
        utterance_gap_s=voice_cfg.LANG_GATE_UTTERANCE_GAP_S,
        short_policy=voice_cfg.LANG_GATE_SHORT,
        min_speech_s=voice_cfg.LANG_GATE_MIN_SPEECH_S,
    )
    logger.info(
        "Language gate %s: language=%s min_prob=%.2f speech=%.1fs (min %.1fs) max_wait=%.1fs short=%s",
        gate.mode, language, gate.min_prob, gate.speech_s, gate.min_speech_s,
        gate.max_wait_s, gate.short_policy,
    )
    if local.is_file():
        threading.Thread(
            target=gate.attach, args=(identifier_for(local),), daemon=True, name="lang-gate-load",
        ).start()
    else:
        gate.store = ModelStore(
            voice_cfg.LANG_ID_CACHE_DIR, local.name, manifest["url"], manifest["sha256"],
            size=manifest["size"],
        )
        gate.store.ensure(lambda path: gate.attach(identifier_for(path)))
    return gate
