"""LanguageIdentifier with the bundled AmberNet model, pinned and unpinned."""

import os
import struct
import threading

import pytest

from hal.drivers.voice._internal import config as voice_cfg
from hal.drivers.voice.lang_id import LanguageIdentifier

pytestmark = pytest.mark.skipif(
    not voice_cfg.LANG_ID_MODEL_PATH.is_file(), reason="AmberNet model not present",
)

TONE = b"".join(struct.pack("<h", int(3000 * ((i // 40) % 2 * 2 - 1))) for i in range(16000))


def make(cpus=None):
    return LanguageIdentifier(
        voice_cfg.LANG_ID_MODEL_PATH, voice_cfg.LANG_ID_LABELS_PATH, ["en", "ja"], cpus=cpus,
    )


def test_unpinned_identify_returns_guess_in_set():
    guess = make().identify(TONE)
    assert guess is not None and guess.language in ("en", "ja")
    assert 0.0 <= guess.in_set <= 1.0


def test_pinned_runs_on_worker_thread():
    identifier = make(cpus=[0])
    seen = []
    original = identifier._logits

    def spy(audio):
        seen.append(threading.current_thread().name)
        return original(audio)

    identifier._logits = spy
    assert identifier.identify(TONE) is not None
    assert seen and seen[0].startswith("lang-id-pinned")


@pytest.mark.skipif(not hasattr(os, "sched_setaffinity"), reason="Linux only")
def test_pinned_worker_affinity_is_applied():
    identifier = make(cpus=[0])
    assert identifier._on_worker(lambda: os.sched_getaffinity(0)) == {0}
