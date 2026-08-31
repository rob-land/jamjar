"""Offline downloads — keep tracks on disk and play them without the server.

Downloads go through `/Audio/{id}/universal?static=true`, which hands
back the original file rather than a transcode, and land in the user
cache dir with a SQLite index beside them.

The index stores enough track metadata to render the Downloaded page
with no network at all. That's the whole point: a library list needs
names and artists, and asking the server for them would make the offline
view work only while online.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from collections.abc import Iterable
from pathlib import Path

from gi.repository import GLib, GObject

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
        # check_same_thread=False: downloads run on the asyncio worker
        # thread and the UI reads from the GTK thread. Writes are short
        # and serialised by SQLite's own locking.
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS audio (
                 item_id      TEXT PRIMARY KEY,
                 path         TEXT NOT NULL,
                 size         INTEGER NOT NULL,
                 added_at     INTEGER NOT NULL,
                 last_played  INTEGER NOT NULL DEFAULT 0
               )"""
        )
        columns = {row[1] for row in self._conn.execute("PRAGMA table_info(audio)")}
        if "meta" not in columns:
            self._conn.execute("ALTER TABLE audio ADD COLUMN meta TEXT")
        self._conn.commit()

    def lookup(self, item_id: str) -> str | None:
        cur = self._conn.execute("SELECT path FROM audio WHERE item_id = ?", (item_id,))
        row = cur.fetchone()
        return row[0] if row else None

    def insert(self, item_id: str, path: str, size: int,
               meta: dict | None = None) -> None:
        self._conn.execute(
            """INSERT INTO audio (item_id, path, size, added_at, meta)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(item_id) DO UPDATE SET
                   path=excluded.path, size=excluded.size,
                   added_at=excluded.added_at, meta=excluded.meta""",
            (item_id, path, size, int(time.time()),
             json.dumps(meta) if meta else None),
        )
        self._conn.commit()

    def entries(self) -> list[dict]:
        """Everything downloaded, newest first, with its stored metadata."""
        rows = self._conn.execute(
            "SELECT item_id, path, size, meta FROM audio ORDER BY added_at DESC"
        ).fetchall()
        out = []
        for item_id, path, size, meta in rows:
            entry = {"item_id": item_id, "path": path, "size": size}
            if meta:
                try:
                    entry.update(json.loads(meta))
                except ValueError:
                    pass
            out.append(entry)
        return out

    def total_bytes(self) -> int:
        row = self._conn.execute("SELECT COALESCE(SUM(size), 0) FROM audio").fetchone()
        return int(row[0])

    def touch(self, item_id: str) -> None:
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

    def remove(self, item_id: str) -> None:
        path = self.lookup(item_id)
        if path:
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass
        self._conn.execute("DELETE FROM audio WHERE item_id = ?", (item_id,))
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()


def track_meta(track: Track) -> dict:
    """The subset of a Track the Downloaded page needs with no server."""
    return {
        "name":            track.name,
        "album":           track.album,
        "album_id":        track.album_id,
        "artists":         list(track.artists),
        "artist_ids":      list(track.artist_ids),
        "duration_ticks":  track.duration_ticks,
        "image_tag":       track.image_tag,
        "album_image_tag": track.album_image_tag,
    }


def track_from_meta(entry: dict) -> Track:
    """Rebuild a playable Track from an index row."""
    return Track(
        id=entry["item_id"],
        name=entry.get("name", ""),
        album=entry.get("album", ""),
        album_id=entry.get("album_id", ""),
        artists=tuple(entry.get("artists") or ()),
        artist_ids=tuple(entry.get("artist_ids") or ()),
        duration_ticks=int(entry.get("duration_ticks") or 0),
        image_tag=entry.get("image_tag"),
        album_image_tag=entry.get("album_image_tag"),
    )


class OfflineManager(GObject.Object):
    """Downloads tracks via /Audio/{id}/universal?static=true."""

    __gtype_name__ = "JamjarOfflineManager"
    __gsignals__ = {
        # Something was downloaded or removed. Args: (item_id,).
        "downloads-changed": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        # Progress through a batch. Args: (done, total).
        "progress":          (GObject.SignalFlags.RUN_FIRST, None, (int, int)),
    }

    def __init__(self, client: JellyfinClient, runner: AsyncRunner,
                 index: OfflineIndex | None = None) -> None:
        super().__init__()
        self.client = client
        self.runner = runner
        self.index = index or OfflineIndex()
        self.cache_dir = _audio_cache_dir()
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._pending: set[str] = set()

    # ------- queries -------

    def entries(self) -> list[dict]:
        return self.index.entries()

    def total_bytes(self) -> int:
        return self.index.total_bytes()

    def is_pending(self, item_id: str) -> bool:
        return item_id in self._pending

    def is_offline(self, item_id: str) -> bool:
        path = self.index.lookup(item_id)
        return path is not None and Path(path).exists()

    def local_path(self, item_id: str) -> str | None:
        return self.index.lookup(item_id)

    def download(self, tracks: Iterable[Track], on_done=None, on_error=None) -> None:
        """Download `tracks` in the background, one at a time.

        Serial by design: a phone on cellular saturating its link with
        parallel downloads makes the track that's actually playing stutter.
        """
        pending = [t for t in tracks if not self.is_offline(t.id)]
        if not pending:
            if on_done:
                GLib.idle_add(lambda: (on_done(), False)[1])
            return
        self._pending.update(t.id for t in pending)
        total = len(pending)

        async def runme():
            for done_count, track in enumerate(pending, start=1):
                try:
                    await self._download_one(track)
                except Exception as e:
                    log.warning("offline download failed for %s: %s", track.id, e)
                    if on_error:
                        GLib.idle_add(lambda e=e, t=track: (on_error(t, e), False)[1])
                finally:
                    self._pending.discard(track.id)
                GLib.idle_add(self._announce, track.id, done_count, total)
            if on_done:
                GLib.idle_add(lambda: (on_done(), False)[1])

        self.runner.submit(runme())

    def _announce(self, item_id: str, done_count: int, total: int) -> bool:
        self.emit("downloads-changed", item_id)
        self.emit("progress", done_count, total)
        return False

    async def _download_one(self, track: Track) -> None:
        if self.is_offline(track.id):
            return
        url = self.client.stream_url(track, codec="copy", static=True)
        target = self.cache_dir / f"{track.id}.audio"
        tmp = target.with_suffix(".part")

        try:
            async with self.client.session.get(url, headers=self.client.headers) as r:
                self.client._check_auth(r)
                r.raise_for_status()
                with open(tmp, "wb") as f:
                    async for chunk in r.content.iter_chunked(64 * 1024):
                        f.write(chunk)
        except Exception:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            raise

        os.replace(tmp, target)
        self.index.insert(track.id, str(target), target.stat().st_size,
                          meta=track_meta(track))

    def remove(self, item_id: str) -> None:
        self.index.remove(item_id)
        self.emit("downloads-changed", item_id)

    def remove_all(self) -> None:
        for entry in self.index.entries():
            self.index.remove(entry["item_id"])
        self.emit("downloads-changed", "")
