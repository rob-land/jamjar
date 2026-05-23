"""Persistent SQLite cache for Jellyfin GET responses (aiohttp-client-cache)."""

from __future__ import annotations

import logging
from datetime import timedelta
from pathlib import Path

from aiohttp_client_cache import CachedSession, SQLiteBackend
from gi.repository import GLib

log = logging.getLogger(__name__)

# Default TTL for paginated library lists (/Items, /Artists/…).
LIBRARY_LIST_TTL = timedelta(hours=1)

# Home shelves and play-history style queries change more often.
HOME_SHELF_TTL = timedelta(minutes=15)
RECENT_PLAY_TTL = timedelta(minutes=5)

# Album tracks, item detail, artist discography, filter metadata.
METADATA_TTL = timedelta(hours=24)

# URL substrings that must never be cached (streams, scrobble, search, art).
_SKIP_CACHE_URLS = {
    "Sessions":     timedelta(seconds=0),
    "Search":       timedelta(seconds=0),
    "InstantMix":   timedelta(seconds=0),
    "Images":       timedelta(seconds=0),
    "Audio":        timedelta(seconds=0),
    "Lyrics":       timedelta(seconds=0),
    "QuickConnect": timedelta(seconds=0),
}


def cache_db_path() -> Path:
    path = Path(GLib.get_user_cache_dir()) / "jamjar" / "http"
    path.mkdir(parents=True, exist_ok=True)
    return path / "responses.sqlite"


def build_cache_backend() -> SQLiteBackend:
    return SQLiteBackend(
        cache_name=str(cache_db_path()),
        expire_after=LIBRARY_LIST_TTL,
        allowed_methods=("GET",),
        allowed_codes=(200,),
        include_headers=True,
        urls_expire_after=_SKIP_CACHE_URLS,
    )


def create_cached_session() -> CachedSession:
    return CachedSession(cache=build_cache_backend())
