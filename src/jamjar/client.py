"""Async Jellyfin REST client + the asyncio-on-bg-thread runner."""

from __future__ import annotations

import asyncio
import logging
import threading
import urllib.parse
from collections.abc import Callable
from concurrent.futures import Future
from datetime import timedelta
from typing import Any

import aiohttp

from .auth import auth_header
from .httpcache import (
    HOME_SHELF_TTL,
    METADATA_TTL,
    RECENT_PLAY_TTL,
    create_cached_session,
)
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
            self._session = create_cached_session()
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

    async def clear_http_cache(self) -> None:
        cache = getattr(self.session, "cache", None)
        if cache is not None:
            await cache.clear()
            log.info("cleared HTTP response cache")

    async def delete_expired_cache(self) -> None:
        cache = getattr(self.session, "cache", None)
        if cache is not None:
            await cache.delete_expired_responses()
            log.debug("pruned expired HTTP cache entries")

    async def _get_json(self, path: str, params: dict | None = None, *,
                        expire_after: timedelta | int | None = None,
                        refresh: bool = False) -> Any:
        async with self.session.get(
            f"{self.base}{path}",
            params=params,
            headers=self.headers,
            expire_after=expire_after,
            refresh=refresh,
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
            if r.content_type and "json" in r.content_type:
                return await r.json()
            if r.content_length:
                return await r.json()
            return None

    async def _delete(self, path: str, params: dict | None = None) -> None:
        async with self.session.delete(
            f"{self.base}{path}", params=params, headers=self.headers
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
        }, expire_after=RECENT_PLAY_TTL)
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
            expire_after=HOME_SHELF_TTL,
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
            expire_after=HOME_SHELF_TTL,
        )
        return [album_from_json(item) for item in data]

    async def item_filters(self, include_item_types: str) -> tuple[list[str], list[int]]:
        """Genres and years present in the library for the given item type."""
        data = await self._get_json("/Items/Filters", params={
            "userId":           self.user_id,
            "IncludeItemTypes": include_item_types,
            "Recursive":        "true",
        }, expire_after=METADATA_TTL)
        genres: list[str] = []
        for item in data.get("GenreItems") or []:
            name = item.get("Name")
            if name:
                genres.append(name)
        if not genres:
            genres = [g for g in data.get("Genres") or [] if g]
        years = sorted(
            {int(y) for y in (data.get("Years") or []) if y is not None},
            reverse=True,
        )
        return genres, years

    async def item_tags(self, include_item_types: str = "Audio") -> list[str]:
        """User-applied tags present in the library, for tag radio stations."""
        data = await self._get_json("/Items/Filters", params={
            "userId":           self.user_id,
            "IncludeItemTypes": include_item_types,
            "Recursive":        "true",
        }, expire_after=METADATA_TTL)
        return sorted({t for t in (data.get("Tags") or []) if t})

    async def random_tracks(self, *, genres: list[str] | None = None,
                            years: list[int] | None = None,
                            tags: list[str] | None = None,
                            favorites: bool = False,
                            limit: int = 100,
                            exclude_ids: set[str] | None = None) -> list[Track]:
        """A random batch of tracks matching a station's filters.

        Never cached: `SortBy=Random` behind the SQLite response cache
        would hand back the same "random" batch on every refill.
        Jellyfin ORs multi-value Genres/Tags on `|`, and Years on `,`.
        """
        params: dict[str, Any] = {
            "userId":                 self.user_id,
            "IncludeItemTypes":       "Audio",
            "Recursive":              "true",
            "SortBy":                 "Random",
            "Limit":                  limit,
            "Fields":                 "MediaSources,AlbumPrimaryImageTag",
            "EnableTotalRecordCount": "false",
        }
        if genres:
            params["Genres"] = "|".join(genres)
        if tags:
            params["Tags"] = "|".join(tags)
        if years:
            params["Years"] = ",".join(str(y) for y in years)
        if favorites:
            params["Filters"] = "IsFavorite"
        data = await self._get_json("/Items", params=params, expire_after=0)
        tracks = [track_from_json(item) for item in data.get("Items", [])]
        if exclude_ids:
            tracks = [t for t in tracks if t.id not in exclude_ids]
        return tracks

    async def list_albums(self, start: int = 0, limit: int = 100,
                          sort_by: str = "SortName",
                          sort_order: str = "Ascending",
                          name_starts_with: str | None = None,
                          name_less_than: str | None = None,
                          genres: str | None = None,
                          years: int | None = None) -> list[Album]:
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
        if genres:
            params["Genres"] = genres
        if years is not None:
            params["Years"] = years
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
                         name_less_than: str | None = None,
                         genres: str | None = None,
                         years: int | None = None) -> list[Track]:
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
        if genres:
            params["Genres"] = genres
        if years is not None:
            params["Years"] = years
        data = await self._get_json("/Items", params=params)
        return [track_from_json(item) for item in data.get("Items", [])]

    async def tracks_by_ids(self, ids: list[str]) -> list[Track]:
        """Look up tracks by item id, in the order asked for.

        Jellyfin returns `Ids` matches in its own sort order, so the
        caller's ordering — a saved queue, say — has to be reapplied here.
        """
        if not ids:
            return []
        data = await self._get_json("/Items", params={
            "userId":                 self.user_id,
            "Ids":                    ",".join(ids),
            "Fields":                 "MediaSources,AlbumPrimaryImageTag",
            "EnableTotalRecordCount": "false",
        }, expire_after=METADATA_TTL)
        found = {t.id: t for t in
                 (track_from_json(item) for item in data.get("Items", []))}
        return [found[i] for i in ids if i in found]

    async def album_tracks(self, album_id: str) -> list[Track]:
        data = await self._get_json("/Items", params={
            "userId":   self.user_id,
            "ParentId": album_id,
            "SortBy":   "ParentIndexNumber,IndexNumber",
            "Fields":   "MediaSources,AlbumPrimaryImageTag",
        }, expire_after=METADATA_TTL)
        return [track_from_json(item) for item in data.get("Items", [])]

    async def artist_albums(self, artist_id: str) -> list[Album]:
        data = await self._get_json("/Items", params={
            "userId":           self.user_id,
            "IncludeItemTypes": "MusicAlbum",
            "AlbumArtistIds":   artist_id,
            "Recursive":        "true",
            "SortBy":           "ProductionYear,SortName",
            "Fields":           "ChildCount,ProductionYear,AlbumArtists",
        }, expire_after=METADATA_TTL)
        return [album_from_json(item) for item in data.get("Items", [])]

    async def playlist_tracks(self, playlist_id: str) -> list[Track]:
        data = await self._get_json(f"/Playlists/{playlist_id}/Items", params={
            "userId": self.user_id,
            "Fields": "MediaSources,AlbumPrimaryImageTag",
        }, expire_after=METADATA_TTL)
        return [track_from_json(item) for item in data.get("Items", [])]

    async def create_playlist(self, name: str,
                              item_ids: list[str] | None = None) -> Playlist:
        data = await self._post_json(
            "/Playlists",
            body={
                "Name":      name,
                "Ids":       item_ids or [],
                "MediaType": "Audio",
            },
            params={"userId": self.user_id},
        )
        return playlist_from_json(data)

    async def add_to_playlist(self, playlist_id: str, item_ids: list[str]) -> None:
        await self._post_json(
            f"/Playlists/{playlist_id}/Items",
            params={
                "userId": self.user_id,
                "ids":    ",".join(item_ids),
            },
        )

    async def update_playlist(self, playlist_id: str, *, name: str) -> None:
        await self._post_json(
            f"/Playlists/{playlist_id}",
            body={"Name": name},
            params={"userId": self.user_id},
        )

    async def delete_playlist(self, playlist_id: str) -> None:
        await self._delete(
            f"/Playlists/{playlist_id}",
            params={"userId": self.user_id},
        )

    async def remove_from_playlist(self, playlist_id: str,
                                  entry_ids: list[str]) -> None:
        await self._delete(
            f"/Playlists/{playlist_id}/Items",
            params={
                "userId":   self.user_id,
                "entryIds": ",".join(entry_ids),
            },
        )

    async def move_playlist_item(self, playlist_id: str, item_id: str,
                                 new_index: int) -> None:
        await self._post_json(
            f"/Playlists/{playlist_id}/Items/{item_id}/Move/{new_index}",
            params={"userId": self.user_id},
        )

    async def instant_mix(self, item_id: str, limit: int = 50) -> list[Track]:
        data = await self._get_json(f"/Items/{item_id}/InstantMix", params={
            "userId":                 self.user_id,
            "Limit":                  limit,
            "Fields":                 "MediaSources,AlbumPrimaryImageTag",
            "EnableTotalRecordCount": "false",
        }, expire_after=0)
        return [track_from_json(item) for item in data.get("Items", [])
                if item.get("Type") == "Audio"]

    async def search(self, query: str, limit: int = 24) -> list[SearchHit]:
        data = await self._get_json("/Search/Hints", params={
            "userId":           self.user_id,
            "searchTerm":       query,
            "IncludeItemTypes": "Audio,MusicAlbum,MusicArtist",
            "Limit":            limit,
        }, expire_after=0)
        return [search_hit_from_json(item) for item in data.get("SearchHints", [])]

    async def get_item(self, item_id: str) -> dict:
        return await self._get_json(
            f"/Users/{self.user_id}/Items/{item_id}",
            params={"Fields": "MediaSources,AlbumPrimaryImageTag"},
            expire_after=METADATA_TTL,
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
