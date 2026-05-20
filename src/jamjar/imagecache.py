"""Persistent on-disk cache for cover and artist artwork.

The cache key is the SHA-256 of the full image URL. The URL already encodes
the item id, the Jellyfin `imageTag`, and the requested `maxWidth`, which
gives us correct behaviour for free:

  - Different sizes (e.g. 144 px tile vs 512 px now-playing cover) get
    distinct entries — both are kept warm.
  - When artwork is updated upstream, Jellyfin re-issues the image with a
    new `imageTag`, the URL changes, and the new fetch naturally misses
    the cache. No explicit invalidation needed.
  - Items deleted from the library simply stop appearing in JSON list
    responses; their cached image bytes linger harmlessly until the LRU
    prune evicts them.

Pruning is mtime-LRU, bounded by `MAX_CACHE_BYTES`, kicked once at app
startup on a daemon thread so it never blocks the GTK loop.
"""

from __future__ import annotations

import hashlib
import logging
import threading
from pathlib import Path

from gi.repository import GLib

log = logging.getLogger(__name__)

MAX_CACHE_BYTES = 200 * 1024 * 1024  # 200 MB

_dir: Path | None = None


def _ensure_dir() -> Path:
    global _dir
    if _dir is None:
        d = Path(GLib.get_user_cache_dir()) / "jamjar" / "images"
        d.mkdir(parents=True, exist_ok=True)
        _dir = d
    return _dir


def _key(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


def get(url: str) -> bytes | None:
    """Return the cached bytes for `url`, or None on miss / I/O failure.

    Touches the file's mtime so frequently-used art stays warm under the
    LRU prune.
    """
    try:
        p = _ensure_dir() / _key(url)
    except OSError:
        return None
    try:
        data = p.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as e:
        log.debug("image cache read failed: %s", e)
        return None
    try:
        p.touch(exist_ok=True)
    except OSError:
        pass
    return data


def put(url: str, data: bytes) -> None:
    """Write bytes for `url`. Best-effort — failures are logged and ignored."""
    if not data:
        return
    try:
        p = _ensure_dir() / _key(url)
    except OSError as e:
        log.debug("image cache dir unavailable: %s", e)
        return
    tmp = p.with_suffix(".tmp")
    try:
        tmp.write_bytes(data)
        tmp.replace(p)
    except OSError as e:
        log.debug("image cache write failed: %s", e)
        try:
            tmp.unlink()
        except OSError:
            pass


def prune(max_bytes: int = MAX_CACHE_BYTES) -> None:
    """Evict oldest entries (by mtime) until total size <= `max_bytes`.

    Also clears any leftover .tmp files from interrupted writes.
    """
    try:
        d = _ensure_dir()
    except OSError:
        return
    entries: list[tuple[float, int, Path]] = []
    total = 0
    try:
        for p in d.iterdir():
            if p.suffix == ".tmp":
                try:
                    p.unlink()
                except OSError:
                    pass
                continue
            try:
                st = p.stat()
            except OSError:
                continue
            entries.append((st.st_mtime, st.st_size, p))
            total += st.st_size
    except OSError as e:
        log.debug("image cache prune iter failed: %s", e)
        return
    if total <= max_bytes:
        return
    entries.sort()  # oldest mtime first
    for _mtime, size, p in entries:
        if total <= max_bytes:
            break
        try:
            p.unlink()
            total -= size
        except OSError:
            continue


def schedule_prune() -> None:
    """Run prune() on a daemon thread — safe to call from app startup."""
    threading.Thread(target=prune, name="jamjar-image-cache-prune",
                     daemon=True).start()
