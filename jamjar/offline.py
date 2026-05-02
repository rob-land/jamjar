"""Offline download manager. v0.3 surface — queues tracks, downloads to cache,
indexes them in SQLite for LRU eviction."""

from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
from pathlib import Path
from typing import Iterable

import aiohttp

import gi
from gi.repository import GLib

from .client import AsyncRunner, JellyfinClient
from .models import Track

log = logging.getLogger(__name__)


def _audio_cache_dir() -> Path:
    return Path(GLib.get_user_cache_dir()) / "jamjar" / "audio"


def _index_path() -> Path:
    return _audio_cache_dir() / "index.sqlite"


class OfflineIndex:
    """Tiny SQLite index for `(item_id, path, size, last_played)`."""

    def __init__(self, path: Path = None) -> None:
        self.path = path or _index_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path))
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS audio (
                 item_id      TEXT PRIMARY KEY,
                 path         TEXT NOT NULL,
                 size         INTEGER NOT NULL,
                 added_at     INTEGER NOT NULL,
                 last_played  INTEGER NOT NULL DEFAULT 0
               )"""
        )
        self._conn.commit()

    def lookup(self, item_id: str) -> str | None:
        cur = self._conn.execute("SELECT path FROM audio WHERE item_id = ?", (item_id,))
        row = cur.fetchone()
        return row[0] if row else None

    def insert(self, item_id: str, path: str, size: int) -> None:
        import time
        self._conn.execute(
            "INSERT OR REPLACE INTO audio (item_id, path, size, added_at) VALUES (?, ?, ?, ?)",
            (item_id, path, size, int(time.time())),
        )
        self._conn.commit()

    def touch(self, item_id: str) -> None:
        import time
        self._conn.execute(
            "UPDATE audio SET last_played = ? WHERE item_id = ?",
            (int(time.time()), item_id),
        )
        self._conn.commit()

    def evict_lru(self, max_bytes: int) -> int:
        cur = self._conn.execute(
            "SELECT item_id, path, size FROM audio ORDER BY last_played ASC, added_at ASC"
        )
        rows = cur.fetchall()
        total = sum(r[2] for r in rows)
        removed = 0
        for item_id, path, size in rows:
            if total <= max_bytes:
                break
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass
            self._conn.execute("DELETE FROM audio WHERE item_id = ?", (item_id,))
            total -= size
            removed += 1
        self._conn.commit()
        return removed

    def close(self) -> None:
        self._conn.close()


class OfflineManager:
    """Downloads tracks via /Audio/{id}/universal?static=true."""

    def __init__(self, client: JellyfinClient, runner: AsyncRunner,
                 index: OfflineIndex | None = None) -> None:
        self.client = client
        self.runner = runner
        self.index = index or OfflineIndex()
        self.cache_dir = _audio_cache_dir()
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def is_offline(self, item_id: str) -> bool:
        path = self.index.lookup(item_id)
        return path is not None and Path(path).exists()

    def local_path(self, item_id: str) -> str | None:
        return self.index.lookup(item_id)

    def download(self, tracks: Iterable[Track], on_done=None, on_error=None) -> None:
        async def runme():
            for track in tracks:
                try:
                    await self._download_one(track)
                except Exception as e:
                    log.warning("offline download failed for %s: %s", track.id, e)
                    if on_error:
                        GLib.idle_add(lambda e=e, t=track: (on_error(t, e), False)[1])
            if on_done:
                GLib.idle_add(lambda: (on_done(), False)[1])

        self.runner.submit(runme())

    async def _download_one(self, track: Track) -> None:
        if self.is_offline(track.id):
            return
        url = self.client.stream_url(track, codec="copy", static=True)
        target = self.cache_dir / f"{track.id}.audio"
        tmp = target.with_suffix(".part")

        async with self.client.session.get(url, headers=self.client.headers) as r:
            r.raise_for_status()
            with open(tmp, "wb") as f:
                async for chunk in r.content.iter_chunked(64 * 1024):
                    f.write(chunk)

        os.replace(tmp, target)
        self.index.insert(track.id, str(target), target.stat().st_size)

    def remove(self, item_id: str) -> None:
        path = self.index.lookup(item_id)
        if path:
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass
        self.index._conn.execute("DELETE FROM audio WHERE item_id = ?", (item_id,))
        self.index._conn.commit()
