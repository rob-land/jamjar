"""High-level library access: caches and exposes Gio.ListModel models for views."""

from __future__ import annotations

import inspect
import logging
from collections.abc import Awaitable, Callable

from gi.repository import Gio, GLib, GObject

from .client import AsyncRunner, JellyfinClient
from .models import Album, Track

log = logging.getLogger(__name__)


class _Wrapper(GObject.Object):
    """Generic GObject wrapper so dataclasses can live in Gio.ListStore.

    `payload` is a `GObject.Property(type=object)` — *not* a plain Python
    attribute — because PyGObject can transparently destroy and recreate the
    Python wrapper around a C GObject (toggle-refs). A plain attribute on the
    Python instance is lost when that happens; a property is stored at the
    C level and survives.
    """

    __gtype_name__ = "JamjarLibraryItem"

    payload = GObject.Property(type=object)

    def __init__(self, payload):
        super().__init__()
        self.payload = payload


def wrap(item) -> _Wrapper:
    return _Wrapper(item)


class WindowedListModel(GObject.Object, Gio.ListModel):
    """A Gio.ListModel that fetches pages from the server on demand.

    The model starts empty. The view (or whoever owns it) calls
    `ensure_first_page()` once it becomes visible; subsequent pages are
    triggered from `maybe_request_more(position)` inside the bind callback,
    which fires whenever the GridView/ColumnView binds a row near the end of
    what's loaded. When a fetch returns fewer items than were requested, the
    model considers itself complete and stops requesting.

    `fetcher` is an async callable `(start: int, limit: int) -> list[T]` —
    typically a bound JellyfinClient method like `client.list_albums`.
    """

    __gtype_name__ = "JamjarWindowedListModel"
    __gsignals__ = {
        # Fired whenever loading starts or finishes (success / empty /
        # error). Views observe this alongside `items-changed` so they can
        # tell "nothing yet because still loading" apart from "nothing
        # because the query genuinely returned empty" — the latter is
        # what should trigger an empty-state UI.
        "load-state-changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    # First fetch is small so the user sees content quickly; subsequent
    # pages are bigger to amortise per-request overhead while scrolling.
    FIRST_PAGE_SIZE = 50
    PAGE_SIZE = 200
    LOOKAHEAD = 40

    def __init__(self, runner: AsyncRunner,
                 fetcher: Callable[..., Awaitable[list]],
                 on_error: Callable[[str], None] | None = None) -> None:
        super().__init__()
        self._runner = runner
        self._fetcher = fetcher
        self._on_error = on_error
        self._items: list[_Wrapper] = []
        self._loading = False
        self._reached_end = False
        # Extra kwargs forwarded to every fetcher call. Used by jump-to-letter
        # to constrain the listing to a name prefix.
        self._filter: dict = {}
        # Bumped on every reset; a fetch's apply() bails if the gen drifted
        # while the request was in flight. Without this, switching letters
        # quickly can let a previous-letter page land on top of the new
        # letter's reset list.
        self._gen = 0

    # ------- Gio.ListModel interface -------

    def do_get_item_type(self):
        return _Wrapper.__gtype__

    def do_get_n_items(self):
        return len(self._items)

    def do_get_item(self, position: int):
        if 0 <= position < len(self._items):
            return self._items[position]
        return None

    # ------- paging control -------

    def ensure_first_page(self) -> None:
        if not self._items and not self._loading:
            self.request_more()

    def maybe_request_more(self, position: int) -> None:
        if self._reached_end or self._loading:
            return
        if position + self.LOOKAHEAD >= len(self._items):
            self.request_more()

    @property
    def is_empty_after_load(self) -> bool:
        """True when the model has finished loading and produced 0 items —
        i.e., the right state to show an empty-state UI."""
        return self._reached_end and not self._items and not self._loading

    def request_more(self) -> None:
        if self._loading or self._reached_end:
            return
        self._loading = True
        self.emit("load-state-changed")
        gen = self._gen
        start = len(self._items)
        limit = self.FIRST_PAGE_SIZE if start == 0 else self.PAGE_SIZE

        filter_kwargs = dict(self._filter)
        # Artists/playlists fetchers ignore sort_*; drop keys they don't accept.
        allowed = set(inspect.signature(self._fetcher).parameters)
        fetch_kwargs = {k: v for k, v in filter_kwargs.items() if k in allowed}

        async def runme():
            return await self._fetcher(start=start, limit=limit, **fetch_kwargs)

        def done(future):
            try:
                items = future.result()
            except Exception as e:
                log.warning("library page fetch failed: %s", e)
                items = []
                error = True
            else:
                error = False

            def apply():
                if gen != self._gen:
                    # A reset() (e.g. filter change) happened while this
                    # request was in flight. Drop the result on the floor.
                    return False
                self._loading = False
                if error:
                    if self._on_error is not None:
                        self._on_error("Couldn't load this section.")
                    self.emit("load-state-changed")
                    return False
                if not items:
                    self._reached_end = True
                    self.emit("load-state-changed")
                    return False
                if len(items) < limit:
                    self._reached_end = True
                base = len(self._items)
                self._items.extend(_Wrapper(it) for it in items)
                self.items_changed(base, 0, len(items))
                self.emit("load-state-changed")
                return False

            GLib.idle_add(apply)

        self._runner.submit(runme()).add_done_callback(done)

    def reset(self) -> None:
        old_n = len(self._items)
        self._items.clear()
        self._loading = False
        self._reached_end = False
        self._gen += 1
        if old_n:
            self.items_changed(0, old_n, 0)
        self.emit("load-state-changed")

    def set_filter(self, **kwargs) -> None:
        """Replace the fetcher's filter kwargs and reload from the start.

        Pass with no arguments to clear the filter. In-flight fetches are
        invalidated by reset()'s gen bump, so their results are dropped if
        they land after the filter has moved on.

        Optional ``sort_by`` and ``sort_order`` are stored in ``_filter`` and
        forwarded to Jellyfin list endpoints that support them.
        """
        new_filter = {k: v for k, v in kwargs.items() if v is not None}
        if new_filter == self._filter and self._items:
            return
        self._filter = new_filter
        self.reset()
        self.ensure_first_page()


class Library(GObject.Object):
    """Caches and exposes Gio.ListStore models for browsing views.

    Instantiated once after login. Views call `load_*` and observe the returned
    `Gio.ListStore` (each entry is a `_Wrapper` whose `payload` is the dataclass).
    """

    __gtype_name__ = "JamjarLibrary"

    def __init__(self, client: JellyfinClient, runner: AsyncRunner,
                 on_error: Callable[[str], None] | None = None) -> None:
        super().__init__()
        self.client = client
        self.runner = runner
        self._on_error = on_error

        self.albums  = WindowedListModel(runner, client.list_albums,  on_error=on_error)
        self.artists = WindowedListModel(runner, client.list_artists, on_error=on_error)
        self.songs   = WindowedListModel(runner, client.list_songs,   on_error=on_error)
        self.playlists = Gio.ListStore.new(_Wrapper)
        self.recently_added = Gio.ListStore.new(_Wrapper)
        self.recently_played = Gio.ListStore.new(_Wrapper)
        self.suggested = Gio.ListStore.new(_Wrapper)

        self._album_cache: dict[str, list[Track]] = {}
        self._artist_cache: dict[str, list[Album]] = {}

    # ------- helpers to push results back to the GTK main loop -------

    def _replace(self, store: Gio.ListStore, items: list) -> None:
        def apply():
            store.remove_all()
            for it in items:
                store.append(_Wrapper(it))
            return False
        GLib.idle_add(apply)

    # ------- top-level loaders -------

    def load_albums(self) -> None:
        self.albums.ensure_first_page()

    def load_artists(self) -> None:
        self.artists.ensure_first_page()

    def load_songs(self) -> None:
        self.songs.ensure_first_page()

    def load_playlists(self) -> None:
        async def runme():
            return await self.client.list_playlists()
        def done(future):
            try:
                items = future.result()
            except Exception as e:
                log.warning("playlists fetch failed: %s", e)
                if self._on_error is not None:
                    self._on_error("Couldn't load playlists.")
                return
            self._replace(self.playlists, items)
        self.runner.submit(runme()).add_done_callback(done)

    def load_recently_added(self) -> None:
        async def runme():
            return await self.client.recently_added(limit=24)
        def done(future):
            try:
                items = future.result()
            except Exception as e:
                log.warning("recently-added fetch failed: %s", e)
                if self._on_error is not None:
                    self._on_error("Couldn't load Recently Added.")
                return
            self._replace(self.recently_added, items)
        self.runner.submit(runme()).add_done_callback(done)

    def load_recently_played(self) -> None:
        async def runme():
            return await self.client.recently_played_tracks(limit=24)
        def done(future):
            try:
                items = future.result()
            except Exception as e:
                log.warning("recently-played fetch failed: %s", e)
                if self._on_error is not None:
                    self._on_error("Couldn't load Recently Played.")
                return
            self._replace(self.recently_played, items)
        self.runner.submit(runme()).add_done_callback(done)

    def load_suggested(self) -> None:
        async def runme():
            return await self.client.suggestions(limit=12)
        def done(future):
            try:
                items = future.result()
            except Exception as e:
                log.warning("suggestions fetch failed: %s", e)
                if self._on_error is not None:
                    self._on_error("Couldn't load Suggestions.")
                return
            self._replace(self.suggested, items)
        self.runner.submit(runme()).add_done_callback(done)

    # ------- per-item loaders -------

    def album_tracks(self, album_id: str, callback) -> None:
        cached = self._album_cache.get(album_id)
        if cached is not None:
            GLib.idle_add(lambda: (callback(cached), False)[1])
            return

        async def runme():
            return await self.client.album_tracks(album_id)

        def done(future):
            tracks = future.result()
            self._album_cache[album_id] = tracks
            GLib.idle_add(lambda: (callback(tracks), False)[1])

        self.runner.submit(runme()).add_done_callback(done)

    def artist_albums(self, artist_id: str, callback) -> None:
        cached = self._artist_cache.get(artist_id)
        if cached is not None:
            GLib.idle_add(lambda: (callback(cached), False)[1])
            return

        async def runme():
            return await self.client.artist_albums(artist_id)

        def done(future):
            albums = future.result()
            self._artist_cache[artist_id] = albums
            GLib.idle_add(lambda: (callback(albums), False)[1])

        self.runner.submit(runme()).add_done_callback(done)

    def search(self, query: str, callback) -> None:
        async def runme():
            return await self.client.search(query)

        def done(future):
            try:
                hits = future.result()
            except Exception as e:
                log.warning("search failed: %s", e)
                hits = []
            GLib.idle_add(lambda: (callback(hits), False)[1])

        self.runner.submit(runme()).add_done_callback(done)
