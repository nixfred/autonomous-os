"""Pinned AmberNet model: resolve a local copy or download it in the background.

The model is too large for the HAL zip (OTA replaces /opt/hal on every update), so
`resources/ambernet.model.json` pins a release asset by URL and SHA-256 and HAL keeps
the verified file in a cache beside the runtime tree (default /opt/.models-hal, like
/opt/.uv-cache-hal), where it survives updates.

A download is written to `<file>.part`, verified, then renamed into place, so the
final path only ever holds a complete, verified model. Until then the language gate
stays inactive and captures pass without a hold.
"""

import hashlib
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger("hal.voice.lang_id")

_CHUNK = 1 << 20
_CONNECT_TIMEOUT_S = 15.0
_READ_TIMEOUT_S = 60.0


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_manifest(path: Path) -> Optional[dict]:
    """{"url", "sha256", "size"?} from the pin file, or None when absent or invalid."""
    try:
        data = json.loads(Path(path).read_text())
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        logger.warning("Language ID: unreadable model manifest %s: %s", path, e)
        return None
    url, sha = str(data.get("url") or ""), str(data.get("sha256") or "").lower()
    if not url.startswith("https://") or len(sha) != 64:
        logger.warning("Language ID: model manifest %s needs an https url and a sha256", path)
        return None
    return {"url": url, "sha256": sha, "size": int(data.get("size") or 0)}


class ModelStore:
    """Makes one pinned model file available; `on_ready(path)` runs once it is verified."""

    def __init__(self, cache_dir: Path, filename: str, url: str, sha256: str, *,
                 size: int = 0, retry_s: float = 30.0, max_retry_s: float = 600.0,
                 fetch: Optional[Callable[[str, Path, threading.Event], None]] = None):
        self.path = Path(cache_dir) / f"{Path(filename).stem}-{sha256[:12]}{Path(filename).suffix}"
        self.url = url
        self.sha256 = sha256
        self.size = size
        self._retry_s = retry_s
        self._max_retry_s = max_retry_s
        self._fetch = fetch or _http_fetch
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def verified(self) -> bool:
        """Whether the cached file exists and matches the pinned hash."""
        if not self.path.is_file():
            return False
        if self.size and self.path.stat().st_size != self.size:
            return False
        return sha256_of(self.path) == self.sha256

    def ensure(self, on_ready: Callable[[Path], None]) -> None:
        """Call on_ready now if the model is cached, else download in the background."""
        self._thread = threading.Thread(
            target=self._run, args=(on_ready,), daemon=True, name="lang-id-model",
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self, on_ready: Callable[[Path], None]) -> None:
        delay = self._retry_s
        while not self._stop.is_set():
            try:
                if self.verified():
                    logger.info("Language ID model ready (%s)", self.path)
                    on_ready(self.path)
                    return
                if self.path.exists():
                    logger.warning("Language ID model %s fails its hash — re-downloading", self.path)
                    self.path.unlink()
                self._download()
                continue  # verify the renamed file on the next pass
            except Exception as e:
                logger.warning("Language ID model download failed (%s); retry in %.0fs", e, delay)
            if self._stop.wait(delay):
                return
            delay = min(delay * 2, self._max_retry_s)

    def _download(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        part = self.path.with_name(self.path.name + ".part")
        started = time.monotonic()
        logger.info("Language ID model downloading %s -> %s", self.url, self.path)
        try:
            self._fetch(self.url, part, self._stop)
            if self._stop.is_set():
                raise RuntimeError("stopped")
            got = sha256_of(part)
            if got != self.sha256:
                raise ValueError(f"sha256 mismatch: got {got[:12]}, want {self.sha256[:12]}")
            os.replace(part, self.path)
        finally:
            part.unlink(missing_ok=True)
        logger.info(
            "Language ID model downloaded (%.1f MB in %.1fs)",
            self.path.stat().st_size / 1e6, time.monotonic() - started,
        )


def _http_fetch(url: str, dest: Path, stop: threading.Event) -> None:
    import requests

    with requests.get(url, stream=True, timeout=(_CONNECT_TIMEOUT_S, _READ_TIMEOUT_S)) as r:
        r.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in r.iter_content(_CHUNK):
                if stop.is_set():
                    return
                f.write(chunk)
