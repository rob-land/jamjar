"""Async Jellyfin REST client + the asyncio-on-bg-thread runner."""

from __future__ import annotations

import asyncio
import logging
import threading
import urllib.parse
from collections.abc import Callable
from concurrent.futures import Future
from typing import Any

import aiohttp

from .auth import auth_header
from .models import (
    Album,
    Artist,
    Playlist,
    SearchHit,
    Track,
    album_from_json,
    artist_from_json,
    playlist_from_json,
    search_hit_from_json,
    track_from_json,
)

log = logging.getLogger(__name__)


class Unauthorized(Exception):
    """Raised when the Jellyfin server returns 401.

    Distinct from a generic ClientResponseError so callers (and the
    application-level handler) can recognise an expired/invalidated token
    without parsing exception args.
    """


class AsyncRunner:
    """A dedicated thread running an asyncio event loop.

    Submit coroutines from the GTK main thread with `submit(coro)`. Marshal the
    result back via `GLib.idle_add` from your callback.
    """

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        self.thread = threading.Thread(target=self._run, name="jamjar-asyncio", daemon=True)
        self.thread.start()
        self._ready.wait()

    def _run(self) -> None:
        # CRITICAL: bind the loop to *this* thread before anything else.
        # Without this, run_coroutine_threadsafe() can misbehave subtly.
        asyncio.set_event_loop(self.loop)
        self._ready.set()
        try:
            self.loop.run_forever()
        finally:
            try:
                pending = asyncio.all_tasks(self.loop)
                for task in pending:
                    task.cancel()
                self.loop.run_until_complete(
                    asyncio.gather(*pending, return_exceptions=True)
                )
            finally:
                self.loop.close()

    def submit(self, coro) -> Future:
        return asyncio.run_coroutine_threadsafe(coro, self.loop)

    def call_soon(self, callback: Callable[[], Any]) -> None:
        self.loop.call_soon_threadsafe(callback)

    def stop(self) -> None:
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(timeout=2.0)


class JellyfinClient:
    """Async REST wrapper around a single Jellyfin user session."""

    def __init__(self, base_url: str, user_id: str, token: str, device_id: str,
                 session: aiohttp.ClientSession | None = None,
                 on_unauthorized: Callable[[], None] | None = None) -> None:
        self.base = base_url.rstrip("/")
        self.user_id = user_id
        self.token = token
        self.device_id = device_id
        self._session = session
        self._owns_session = session is None
        # Optional sink for 401 responses. Called from the asyncio thread
        # — implementers must marshal back to GTK with GLib.idle_add.
        # May be invoked many times in quick succession (parallel requests
        # all 401ing), so the handler must be idempotent.
        self.on_unauthorized = on_unauthorized

    async def __aenter__(self) -> JellyfinClient:
        if self._session is None:
            self._session = aiohttp.ClientSession()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()

    async def close(self) -> None:
        if self._owns_session and self._session is not None:
            await self._session.close()
            self._session = None

    @property
    def session(self) -> aiohttp.ClientSession:
        if self._session is None:
            raise RuntimeError("JellyfinClient used outside its lifecycle")
        return self._session

    @property
    def headers(self) -> dict[str, str]:
        return auth_header(self.device_id, self.token)

    # ------- low-level helpers -------

    def _check_auth(self, r: aiohttp.ClientResponse) -> None:
        if r.status != 401:
            return
        if self.on_unauthorized is not None:
            try:
                self.on_unauthorized()
            except Exception:
                log.exception("on_unauthorized handler raised")
        raise Unauthorized(f"401 Unauthorized for {r.url}")

    async def _get_json(self, path: str, params: dict | None = None) -> Any:
        async with self.session.get(
            f"{self.base}{path}", params=params, headers=self.headers
        ) as r:
            self._check_auth(r)
            r.raise_for_status()
            return await r.json()

    async def _post_json(self, path: str, body: dict | None = None,
                         params: dict | None = None) -> Any:
        async with self.session.post(
            f"{self.base}{path}", json=body, params=params, headers=self.headers
        ) as r:
            self._check_auth(r)
            r.raise_for_status()
            if r.content_length:
                return await r.json()
            return None

    async def _delete(self, path: str) -> None:
        async with self.session.delete(
            f"{self.base}{path}", headers=self.headers
        ) as r:
            self._check_auth(r)
            r.raise_for_status()

    # ------- library queries -------

    async def user_views(self) -> list[dict]:
        data = await self._get_json("/UserViews", params={"userId": self.user_id})
        return data.get("Items", [])

    async def recently_played_tracks(self, limit: int = 24) -> list[Track]:
        # Jellyfin doesn't expose a dedicated "recently played" endpoint —
        # the convention is to query /Items sorted by DatePlayed descending,
        # filtered to items the user has actually played.
        data = await self._get_json("/Items", params={
            "userId":                 self.user_id,
            "IncludeItemTypes":       "Audio",
            "Recursive":              "true",
            "SortBy":                 "DatePlayed",
            "SortOrder":              "Descending",
            "Filters":                "IsPlayed",
            "Limit":                  limit,
            "Fields":                 "AlbumPrimaryImageTag",
            "EnableTotalRecordCount": "false",
        })
        return [track_from_json(item) for item in data.get("Items", [])]

    async def suggestions(self, limit: int = 12) -> list[Track]:
        data = await self._get_json(
            f"/Users/{self.user_id}/Suggestions",
            params={
                "mediaType":              "Audio",
                "limit":                  limit,
                "enableTotalRecordCount": "false",
                "fields":                 "AlbumPrimaryImageTag",
            },
        )
        # Suggestions may include non-Audio items depending on server
        # config; defensive filter so we never try to render an album
        # tile through the track tile widget.
        return [track_from_json(item) for item in data.get("Items", [])
                if item.get("Type") == "Audio"]

    async def recently_added(self, limit: int = 24) -> list[Album]:
        # GroupItems=true (the default) returns one MusicAlbum per recent
        # audio batch, which is what we want — the home row visualises
        # albums, not individual tracks.
        data = await self._get_json(
            f"/Users/{self.user_id}/Items/Latest",
            params={"IncludeItemTypes": "Audio", "Limit": limit},
        )
        return [album_from_json(item) for item in data]

    async def list_albums(self, start: int = 0, limit: int = 100,
                          sort_by: str = "SortName",
                          sort_order: str = "Ascending",
                          name_starts_with: str | None = None,
                          name_less_than: str | None = None) -> list[Album]:
        params: dict[str, Any] = {
            "userId":                 self.user_id,
            "IncludeItemTypes":       "MusicAlbum",
            "Recursive":              "true",
            "StartIndex":             start,
            "Limit":                  limit,
            "SortBy":                 sort_by,
            "SortOrder":              sort_order,
            "Fields":                 "ChildCount,ProductionYear,AlbumArtists",
            "EnableTotalRecordCount": "false",
        }
        if name_starts_with:
            params["NameStartsWith"] = name_starts_with
        if name_less_than:
            params["NameLessThan"] = name_less_than
        data = await self._get_json("/Items", params=params)
        return [album_from_json(item) for item in data.get("Items", [])]

    async def list_artists(self, start: int = 0, limit: int = 100,
                           name_starts_with: str | None = None,
                           name_less_than: str | None = None) -> list[Artist]:
        # /Artists/AlbumArtists rather than /Artists: returns only artists
        # who appear as the album artist of at least one album, suppressing
        # the long tail of single-track guest features that clutter
        # /Artists. Featured / track artists remain reachable via search
        # and (eventually) clickable artist names on track rows.
        params: dict[str, Any] = {
            "userId":                 self.user_id,
            "Recursive":              "true",
            "StartIndex":             start,
            "Limit":                  limit,
            "SortBy":                 "SortName",
            "EnableTotalRecordCount": "false",
        }
        if name_starts_with:
            params["NameStartsWith"] = name_starts_with
        if name_less_than:
            params["NameLessThan"] = name_less_than
        data = await self._get_json("/Artists/AlbumArtists", params=params)
        return [artist_from_json(item) for item in data.get("Items", [])]

    async def list_playlists(self, start: int = 0, limit: int = 100) -> list[Playlist]:
        data = await self._get_json("/Items", params={
            "userId":                 self.user_id,
            "IncludeItemTypes":       "Playlist",
            "Recursive":              "true",
            "StartIndex":             start,
            "Limit":                  limit,
            "SortBy":                 "SortName",
            "EnableTotalRecordCount": "false",
        })
        return [playlist_from_json(item) for item in data.get("Items", [])]

    async def list_songs(self, start: int = 0, limit: int = 200,
                         sort_by: str = "SortName",
                         name_starts_with: str | None = None,
                         name_less_than: str | None = None) -> list[Track]:
        # MediaSources is intentionally omitted here: the songs list only needs
        # display fields. MediaSources is fetched on demand at play time
        # via get_item(), and is the heaviest field Jellyfin can return.
        params: dict[str, Any] = {
            "userId":                 self.user_id,
            "IncludeItemTypes":       "Audio",
            "Recursive":              "true",
            "StartIndex":             start,
            "Limit":                  limit,
            "SortBy":                 sort_by,
            "Fields":                 "AlbumPrimaryImageTag",
            "EnableTotalRecordCount": "false",
        }
        if name_starts_with:
            params["NameStartsWith"] = name_starts_with
        if name_less_than:
            params["NameLessThan"] = name_less_than
        data = await self._get_json("/Items", params=params)
        return [track_from_json(item) for item in data.get("Items", [])]

    async def album_tracks(self, album_id: str) -> list[Track]:
        data = await self._get_json("/Items", params={
            "userId":   self.user_id,
            "ParentId": album_id,
            "SortBy":   "ParentIndexNumber,IndexNumber",
            "Fields":   "MediaSources,AlbumPrimaryImageTag",
        })
        return [track_from_json(item) for item in data.get("Items", [])]

    async def artist_albums(self, artist_id: str) -> list[Album]:
        data = await self._get_json("/Items", params={
            "userId":           self.user_id,
            "IncludeItemTypes": "MusicAlbum",
            "AlbumArtistIds":   artist_id,
            "Recursive":        "true",
            "SortBy":           "ProductionYear,SortName",
            "Fields":           "ChildCount,ProductionYear,AlbumArtists",
        })
        return [album_from_json(item) for item in data.get("Items", [])]

    async def playlist_tracks(self, playlist_id: str) -> list[Track]:
        data = await self._get_json(f"/Playlists/{playlist_id}/Items", params={
            "userId": self.user_id,
            "Fields": "MediaSources,AlbumPrimaryImageTag",
        })
        return [track_from_json(item) for item in data.get("Items", [])]

    async def instant_mix(self, item_id: str, limit: int = 50) -> list[Track]:
        data = await self._get_json(f"/Items/{item_id}/InstantMix", params={
            "userId":                 self.user_id,
            "Limit":                  limit,
            "Fields":                 "MediaSources,AlbumPrimaryImageTag",
            "EnableTotalRecordCount": "false",
        })
        return [track_from_json(item) for item in data.get("Items", [])
                if item.get("Type") == "Audio"]

    async def search(self, query: str, limit: int = 24) -> list[SearchHit]:
        data = await self._get_json("/Search/Hints", params={
            "userId":           self.user_id,
            "searchTerm":       query,
            "IncludeItemTypes": "Audio,MusicAlbum,MusicArtist",
            "Limit":            limit,
        })
        return [search_hit_from_json(item) for item in data.get("SearchHints", [])]

    async def get_item(self, item_id: str) -> dict:
        return await self._get_json(
            f"/Users/{self.user_id}/Items/{item_id}",
            params={"Fields": "MediaSources,AlbumPrimaryImageTag"},
        )

    # ------- favorites -------

    async def set_favorite(self, item_id: str, is_favorite: bool) -> None:
        path = f"/Users/{self.user_id}/FavoriteItems/{item_id}"
        if is_favorite:
            await self._post_json(path)
        else:
            await self._delete(path)

    # ------- playback reporting -------

    async def report_playing(self, body: dict) -> None:
        await self._post_json("/Sessions/Playing", body)

    async def report_progress(self, body: dict) -> None:
        await self._post_json("/Sessions/Playing/Progress", body)

    async def report_stopped(self, body: dict) -> None:
        await self._post_json("/Sessions/Playing/Stopped", body)

    # ------- URLs (synchronous helpers; safe to call from GTK thread) -------

    def stream_url(self, track: Track, *, codec: str = "copy",
                   max_bitrate: int = 0, static: bool = False) -> str:
        params = {
            "api_key":          self.token,
            "userId":           self.user_id,
            "deviceId":         self.device_id,
            "audioCodec":       codec,
        }
        if max_bitrate:
            params["maxStreamingBitrate"] = str(max_bitrate)
        if static:
            params["static"] = "true"
        query = urllib.parse.urlencode(params)
        return f"{self.base}/Audio/{track.id}/universal?{query}"

    def cover_url(self, item_id: str, tag: str | None = None,
                  max_width: int = 512) -> str:
        params: dict[str, str] = {"maxWidth": str(max_width)}
        if tag:
            params["tag"] = tag
        query = urllib.parse.urlencode(params)
        return f"{self.base}/Items/{item_id}/Images/Primary?{query}"

    def lyrics_url(self, track: Track) -> str:
        return f"{self.base}/Audio/{track.id}/Lyrics"
