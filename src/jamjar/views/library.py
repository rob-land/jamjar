"""Library tab — Albums / Artists / Songs / Playlists."""

from __future__ import annotations

import logging
from gettext import gettext as _
from typing import TYPE_CHECKING

from gi.repository import Adw, GLib, Gtk

from ._common import (
    clear_remote_image,
    escape_markup,
    format_duration,
    load_remote_image_async,
)
from .album_menu import install_album_menu
from .track_menu import install_track_menu

if TYPE_CHECKING:
    from ..application import JamjarApplication
    from ..window import JamjarWindow

log = logging.getLogger(__name__)

# Jellyfin SortBy values for library tabs that support reordering.
_ALBUM_SORTS: list[tuple[str, str]] = [
    ("SortName",        "Title"),
    ("ProductionYear",  "Year"),
    ("DateCreated",     "Recently added"),
    ("Random",          "Shuffle"),
]
_SONG_SORTS: list[tuple[str, str]] = [
    ("SortName",             "Title"),
    ("AlbumArtist,Artist",   "Artist"),
    ("Album",                "Album"),
    ("DatePlayed",           "Recently played"),
    ("Random",               "Shuffle"),
]
_SORT_TABS = frozenset({"albums", "songs"})
_FILTER_TABS = _SORT_TABS
_FILTER_ITEM_TYPES = {
    "albums": "MusicAlbum",
    "songs":  "Audio",
}


@Gtk.Template(resource_path="/land/rob/jamjar/library-page.ui")
class LibraryPage(Adw.NavigationPage):
    __gtype_name__ = "JamjarLibraryPage"

    LETTER_TABS = ("albums", "artists", "songs")

    sidebar_toggle  = Gtk.Template.Child()
    stack           = Gtk.Template.Child()
    refresh_button  = Gtk.Template.Child()
    filter_button   = Gtk.Template.Child()
    sort_button     = Gtk.Template.Child()
    letter_button   = Gtk.Template.Child()
    albums_grid     = Gtk.Template.Child()
    artists_grid    = Gtk.Template.Child()
    songs_list      = Gtk.Template.Child()
    playlists_list  = Gtk.Template.Child()
    albums_stack    = Gtk.Template.Child()
    artists_stack   = Gtk.Template.Child()
    songs_stack     = Gtk.Template.Child()
    playlists_stack = Gtk.Template.Child()

    def __init__(self, app: JamjarApplication, window: JamjarWindow) -> None:
        super().__init__()
        self.app = app
        self.window = window
        self._loaded: set[str] = set()
        # Per-tab active letter filter. None = no filter (label "A-Z"),
        # "#" = names that sort before "A" (digits/symbols), otherwise the
        # uppercase letter prefix being filtered on.
        self._tab_letters: dict[str, str | None] = {n: None for n in self.LETTER_TABS}
        self._tab_sorts: dict[str, str] = {
            "albums": _ALBUM_SORTS[0][0],
            "songs":  _SONG_SORTS[0][0],
        }
        self._tab_genres: dict[str, str | None] = {n: None for n in _FILTER_TABS}
        self._tab_years: dict[str, int | None] = {n: None for n in _FILTER_TABS}
        self._filters_cache: dict[str, tuple[list[str], list[int]]] = {}
        self._filters_loading = False
        self.sidebar_toggle.connect("clicked", lambda *_: self.window.toggle_sidebar())
        self.refresh_button.connect("clicked", self._on_refresh)
        self._wire_filter_button()
        self._wire_sort_button()
        self._wire_albums()
        self._wire_artists()
        self._wire_songs()
        self._wire_playlists()
        self._wire_letter_button()
        self.stack.connect("notify::visible-child-name", self._on_tab_changed)
        GLib.idle_add(self._load_visible_tab)

    def _load_visible_tab(self) -> bool:
        if self.app.library is None:
            return False
        name = self.stack.get_visible_child_name()
        if name:
            self._load_tab(name)
        # Prefetch the other tabs after a short delay so the visible tab
        # gets its request out first (and rendered before the others land).
        # Without this, switching to Artists / Songs incurs a fresh
        # roundtrip every time and feels noticeably slower than the
        # already-loaded Albums tab.
        GLib.timeout_add(400, self._prefetch_other_tabs)
        return False

    def _prefetch_other_tabs(self) -> bool:
        if self.app.library is None:
            return False
        for name in ("albums", "artists", "songs", "playlists"):
            self._load_tab(name)
        return False

    def _on_tab_changed(self, stack, _pspec) -> None:
        name = stack.get_visible_child_name()
        if name:
            self._load_tab(name)
        self._refresh_letter_button()
        self._refresh_sort_button()
        self._refresh_filter_button()

    def _on_refresh(self, _btn) -> None:
        if self.app.library is None:
            return
        self._loaded.clear()
        self.app.library.refresh_all()
        self._load_visible_tab()
        if self.app.show_toast:
            self.app.show_toast(_("Refreshing library…"))
        self._filters_cache.clear()

    # ------- genre / year filter -------

    def _wire_filter_button(self) -> None:
        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_max_content_height(420)
        scrolled.set_propagate_natural_height(True)
        self._filter_box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            margin_top=8, margin_bottom=8, margin_start=8, margin_end=8,
            spacing=8,
        )
        scrolled.set_child(self._filter_box)
        self._filter_popover = Gtk.Popover()
        self._filter_popover.set_child(scrolled)
        self.filter_button.set_popover(self._filter_popover)
        self._filter_popover.connect("show", self._on_filter_popover_show)
        self.stack.connect("notify::visible-child-name",
                           lambda *_: self._refresh_filter_button())

    def _on_filter_popover_show(self, _popover) -> None:
        self._ensure_filters_loaded()

    def _ensure_filters_loaded(self) -> None:
        name = self.stack.get_visible_child_name()
        if name not in _FILTER_TABS or self.app.client is None:
            self._rebuild_filter_popover()
            return
        if name in self._filters_cache:
            self._rebuild_filter_popover()
            return
        if self._filters_loading:
            return
        self._filters_loading = True
        item_type = _FILTER_ITEM_TYPES[name]

        async def runme():
            return await self.app.client.item_filters(item_type)

        def done(future):
            self._filters_loading = False
            try:
                options = future.result()
            except Exception as e:
                log.warning("library filters fetch failed: %s", e)
                options = ([], [])
                if self.app.show_toast:
                    self.app.show_toast(_("Couldn't load filters."))
            self._filters_cache[name] = options
            GLib.idle_add(self._rebuild_filter_popover)

        self.app.runner.submit(runme()).add_done_callback(done)
        self._rebuild_filter_popover(loading=True)

    def _rebuild_filter_popover(self, *, loading: bool = False) -> None:
        for child in list(self._filter_box):
            self._filter_box.remove(child)

        name = self.stack.get_visible_child_name()
        if name not in _FILTER_TABS:
            return

        if loading and name not in self._filters_cache:
            self._filter_box.append(Gtk.Label(label=_("Loading…"), xalign=0))
            return

        genres, years = self._filters_cache.get(name, ([], []))
        active_genre = self._tab_genres.get(name)
        active_year = self._tab_years.get(name)

        clear = Gtk.Button(label=_("Clear filters"))
        clear.add_css_class("flat")
        clear.set_sensitive(bool(active_genre or active_year is not None))
        clear.connect("clicked", self._on_clear_filters)
        self._filter_box.append(clear)

        genre_label = Gtk.Label(label=_("Genre"), xalign=0)
        genre_label.add_css_class("heading")
        self._filter_box.append(genre_label)

        for label, value in ((_("All genres"), None), *[(g, g) for g in genres]):
            btn = Gtk.Button(label=label)
            btn.add_css_class("flat")
            if value == active_genre or (value is None and active_genre is None):
                btn.add_css_class("suggested-action")
            btn.connect("clicked", self._on_genre_clicked, value)
            self._filter_box.append(btn)

        if not genres and not loading:
            hint = Gtk.Label(
                label=_("No genres in your library — tag media in Jellyfin."),
                xalign=0,
                wrap=True,
                wrap_mode=2,
            )
            hint.add_css_class("dim-label")
            hint.add_css_class("caption")
            self._filter_box.append(hint)

        year_label = Gtk.Label(label=_("Year"), xalign=0)
        year_label.add_css_class("heading")
        self._filter_box.append(year_label)

        year_grid = Gtk.Grid(column_spacing=4, row_spacing=4)
        year_choices: list[tuple[str, int | None]] = [(_("All years"), None)]
        year_choices.extend((str(y), y) for y in years)
        cols = 4
        for i, (label, value) in enumerate(year_choices):
            btn = Gtk.Button(label=label)
            btn.add_css_class("flat")
            if value == active_year or (value is None and active_year is None):
                btn.add_css_class("suggested-action")
            btn.connect("clicked", self._on_year_clicked, value)
            year_grid.attach(btn, i % cols, i // cols, 1, 1)
        self._filter_box.append(year_grid)

        if not years:
            self._filter_box.append(Gtk.Label(
                label=_("No release years available."),
                xalign=0,
            ))

    def _on_clear_filters(self, _btn) -> None:
        name = self.stack.get_visible_child_name()
        if name not in _FILTER_TABS:
            return
        self._tab_genres[name] = None
        self._tab_years[name] = None
        self._apply_tab_filter(name)
        self._loaded.add(name)
        self._filter_popover.popdown()
        self._rebuild_filter_popover()
        self._refresh_filter_button()

    def _on_genre_clicked(self, _btn, genre: str | None) -> None:
        name = self.stack.get_visible_child_name()
        if name not in _FILTER_TABS:
            return
        self._tab_genres[name] = genre
        self._apply_tab_filter(name)
        self._loaded.add(name)
        self._rebuild_filter_popover()
        self._refresh_filter_button()

    def _on_year_clicked(self, _btn, year: int | None) -> None:
        name = self.stack.get_visible_child_name()
        if name not in _FILTER_TABS:
            return
        self._tab_years[name] = year
        self._apply_tab_filter(name)
        self._loaded.add(name)
        self._rebuild_filter_popover()
        self._refresh_filter_button()

    def _refresh_filter_button(self) -> None:
        name = self.stack.get_visible_child_name()
        if name not in _FILTER_TABS:
            self.filter_button.set_sensitive(False)
            self.filter_button.remove_css_class("suggested-action")
            self.filter_button.set_tooltip_text(_("Filter"))
            return
        self.filter_button.set_sensitive(True)
        parts: list[str] = []
        genre = self._tab_genres.get(name)
        year = self._tab_years.get(name)
        if genre:
            parts.append(genre)
        if year is not None:
            parts.append(str(year))
        if parts:
            self.filter_button.add_css_class("suggested-action")
            self.filter_button.set_tooltip_text(_("Filter: %s") % ", ".join(parts))
        else:
            self.filter_button.remove_css_class("suggested-action")
            self.filter_button.set_tooltip_text(_("Filter"))

    # ------- sort -------

    def _wire_sort_button(self) -> None:
        self._sort_popover = Gtk.Popover()
        self._sort_box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            margin_top=8, margin_bottom=8, margin_start=8, margin_end=8,
            spacing=4,
        )
        self._sort_popover.set_child(self._sort_box)
        self.sort_button.set_popover(self._sort_popover)
        self.stack.connect("notify::visible-child-name",
                           lambda *_: self._rebuild_sort_popover())
        self._rebuild_sort_popover()

    def _rebuild_sort_popover(self) -> None:
        for child in list(self._sort_box):
            self._sort_box.remove(child)
        name = self.stack.get_visible_child_name()
        if name not in _SORT_TABS:
            return
        options = _ALBUM_SORTS if name == "albums" else _SONG_SORTS
        active = self._tab_sorts.get(name, "SortName")
        for sort_key, label in options:
            btn = Gtk.Button(label=label)
            btn.add_css_class("flat")
            if sort_key == active:
                btn.add_css_class("suggested-action")
            btn.connect("clicked", self._on_sort_clicked, sort_key)
            self._sort_box.append(btn)

    def _on_sort_clicked(self, _btn, sort_key: str) -> None:
        self._sort_popover.popdown()
        name = self.stack.get_visible_child_name()
        if name not in _SORT_TABS:
            return
        self._tab_sorts[name] = sort_key
        self._apply_tab_filter(name)
        self._loaded.add(name)
        self._rebuild_sort_popover()
        self._refresh_sort_button()

    def _filter_kwargs_for_tab(self, tab: str) -> dict:
        kwargs: dict = {}
        if tab in _SORT_TABS:
            kwargs["sort_by"] = self._tab_sorts.get(tab, "SortName")
        letter = self._tab_letters.get(tab)
        if letter == "#":
            kwargs["name_less_than"] = "A"
        elif letter:
            kwargs["name_starts_with"] = letter
        genre = self._tab_genres.get(tab)
        if genre:
            kwargs["genres"] = genre
        year = self._tab_years.get(tab)
        if year is not None:
            kwargs["years"] = year
        return kwargs

    def _apply_tab_filter(self, tab: str) -> None:
        if self.app.library is None or tab not in self.LETTER_TABS:
            return
        model = {
            "albums":  self.app.library.albums,
            "artists": self.app.library.artists,
            "songs":   self.app.library.songs,
        }[tab]
        model.set_filter(**self._filter_kwargs_for_tab(tab))

    def _refresh_sort_button(self) -> None:
        name = self.stack.get_visible_child_name()
        if name not in _SORT_TABS:
            self.sort_button.set_sensitive(False)
            self.sort_button.set_tooltip_text(_("Sort"))
            return
        self.sort_button.set_sensitive(True)
        options = _ALBUM_SORTS if name == "albums" else _SONG_SORTS
        sort_key = self._tab_sorts.get(name, "SortName")
        label = next((lbl for key, lbl in options if key == sort_key), "Title")
        self.sort_button.set_tooltip_text(_("Sort: %s") % label)

    # ------- jump-to-letter -------

    def _wire_letter_button(self) -> None:
        self.letter_button.set_popover(self._build_letter_popover())
        self._refresh_letter_button()

    def _build_letter_popover(self) -> Gtk.Popover:
        # 7-column grid: row 0 = "All" + "#" + A-E (7), then F-L, M-S, T-Z.
        # 28 cells total fits A-Z + the two specials cleanly at 7×4.
        chars = ["All", "#"] + [chr(c) for c in range(ord("A"), ord("Z") + 1)]
        cols = 7

        popover = Gtk.Popover()
        grid = Gtk.Grid(row_spacing=4, column_spacing=4,
                        margin_top=8, margin_bottom=8,
                        margin_start=8, margin_end=8)
        for i, ch in enumerate(chars):
            btn = Gtk.Button(label=ch)
            btn.add_css_class("flat")
            btn.set_size_request(36, 36)
            btn.connect("clicked", self._on_letter_clicked, ch, popover)
            grid.attach(btn, i % cols, i // cols, 1, 1)
        popover.set_child(grid)
        return popover

    def _on_letter_clicked(self, _btn, choice: str, popover: Gtk.Popover) -> None:
        popover.popdown()
        name = self.stack.get_visible_child_name()
        if name not in self.LETTER_TABS or self.app.library is None:
            return

        if choice == "All":
            self._tab_letters[name] = None
        elif choice == "#":
            self._tab_letters[name] = "#"
        else:
            self._tab_letters[name] = choice

        self._apply_tab_filter(name)
        self._loaded.add(name)
        self._refresh_letter_button()

    def _refresh_letter_button(self) -> None:
        name = self.stack.get_visible_child_name()
        if name not in self.LETTER_TABS:
            self.letter_button.set_sensitive(False)
            self.letter_button.set_label("A-Z")
            return
        self.letter_button.set_sensitive(True)
        active = self._tab_letters.get(name)
        self.letter_button.set_label(active if active else "A-Z")

    def _load_tab(self, name: str) -> None:
        if name in self._loaded:
            return
        lib = self.app.library
        if lib is None:
            return
        loader = {
            "albums":    lib.load_albums,
            "artists":   lib.load_artists,
            "songs":     lib.load_songs,
            "playlists": lib.load_playlists,
        }.get(name)
        if loader is None:
            return
        self._loaded.add(name)
        loader()

    # ------- albums -------

    def _wire_albums(self) -> None:
        store = self.app.library.albums
        factory = Gtk.SignalListItemFactory()
        factory.connect("setup", self._album_setup)
        factory.connect("bind",  self._album_bind)
        self.albums_grid.set_model(Gtk.NoSelection.new(store))
        self.albums_grid.set_factory(factory)
        self.albums_grid.connect("activate", self._album_activated)
        self._observe_windowed(store, self.albums_stack)

    def _album_setup(self, _factory, item) -> None:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6,
                      margin_top=4, margin_bottom=4, margin_start=4, margin_end=4)
        picture = Gtk.Picture(content_fit=Gtk.ContentFit.COVER,
                              width_request=144, height_request=144)
        picture.add_css_class("cover")
        title = Gtk.Label(ellipsize=3, xalign=0)
        title.add_css_class("heading")
        artist = Gtk.Label(ellipsize=3, xalign=0)
        artist.add_css_class("dim-label")
        artist.add_css_class("caption")
        box.append(picture)
        box.append(title)
        box.append(artist)
        item.set_child(box)
        item.picture, item.title_label, item.artist_label = picture, title, artist
        # GridView recycles tile widgets across albums, so the menu reads the
        # currently-bound payload via a getter rather than capturing one.
        install_album_menu(box,
                           lambda it=item: (it.get_item().payload
                                             if it.get_item() else None),
                           self.app, self.window)

    def _album_bind(self, _factory, item) -> None:
        album = item.get_item().payload
        item.title_label.set_label(album.name)
        item.artist_label.set_label(album.primary_artist)
        if album.image_tag:
            url = self.app.client.cover_url(album.id, album.image_tag, max_width=256)
            load_remote_image_async(url, self.app.client.headers, item.picture,
                                    self.app.client.session, self.app.runner)
        else:
            clear_remote_image(item.picture)
        self.app.library.albums.maybe_request_more(item.get_position())

    def _album_activated(self, grid, position) -> None:
        model = grid.get_model()
        wrapper = model.get_item(position) if model else None
        if wrapper is None:
            log.warning("album activated at position %s but no item", position)
            return
        self.window.open_album(wrapper.payload)

    # ------- artists -------

    def _wire_artists(self) -> None:
        store = self.app.library.artists
        factory = Gtk.SignalListItemFactory()
        factory.connect("setup", self._artist_setup)
        factory.connect("bind",  self._artist_bind)
        self.artists_grid.set_model(Gtk.NoSelection.new(store))
        self.artists_grid.set_factory(factory)
        self.artists_grid.connect("activate", self._artist_activated)
        self._observe_windowed(store, self.artists_stack)

    def _artist_setup(self, _factory, item) -> None:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6,
                      margin_top=4, margin_bottom=4, margin_start=4, margin_end=4)
        picture = Gtk.Picture(content_fit=Gtk.ContentFit.COVER,
                              width_request=144, height_request=144)
        picture.add_css_class("cover")
        name = Gtk.Label(ellipsize=3, xalign=0)
        name.add_css_class("heading")
        box.append(picture)
        box.append(name)
        item.set_child(box)
        item.picture, item.name_label = picture, name

    def _artist_bind(self, _factory, item) -> None:
        artist = item.get_item().payload
        item.name_label.set_label(artist.name)
        if artist.image_tag:
            url = self.app.client.cover_url(artist.id, artist.image_tag, max_width=256)
            load_remote_image_async(url, self.app.client.headers, item.picture,
                                    self.app.client.session, self.app.runner)
        else:
            clear_remote_image(item.picture)
        self.app.library.artists.maybe_request_more(item.get_position())

    def _artist_activated(self, grid, position) -> None:
        model = grid.get_model()
        wrapper = model.get_item(position) if model else None
        if wrapper is None:
            log.warning("artist activated at position %s but no item", position)
            return
        self.window.open_artist(wrapper.payload)

    # ------- songs -------

    def _wire_songs(self) -> None:
        store = self.app.library.songs
        selection = Gtk.NoSelection.new(store)
        self.songs_list.set_model(selection)
        self._observe_windowed(store, self.songs_stack)

        factory = Gtk.SignalListItemFactory()
        factory.connect("setup", self._song_setup)
        factory.connect("bind", self._song_bind)
        self.songs_list.set_factory(factory)
        self.songs_list.connect("activate", self._song_activated)

    def _song_setup(self, _factory, item) -> None:
        row = Adw.ActionRow(activatable=True)
        duration = Gtk.Label(xalign=1)
        duration.add_css_class("dim-label")
        duration.add_css_class("numeric")
        row.add_suffix(duration)
        item.set_child(row)
        item.row = row
        item.duration_label = duration
        install_track_menu(row,
                           lambda it=item: (it.get_item().payload
                                            if it.get_item() else None),
                           self.app, self.window)

    def _song_bind(self, _factory, item) -> None:
        track = item.get_item().payload
        item.row.set_title(escape_markup(track.name))
        item.row.set_subtitle(escape_markup(
            " • ".join(part for part in (track.primary_artist, track.album) if part)
        ))
        item.duration_label.set_label(format_duration(track.duration_seconds))
        self.app.library.songs.maybe_request_more(item.get_position())

    def _song_activated(self, view, position) -> None:
        model = view.get_model()
        wrapper = model.get_item(position) if model else None
        if wrapper is None:
            log.warning("song activated at position %s but no item", position)
            return
        store = self.app.library.songs
        tracks = [store.get_item(i).payload for i in range(store.get_n_items())]
        self.app.queue.replace(tracks, start_index=position)
        self.app.player.play(wrapper.payload)

    # ------- playlists -------

    def _wire_playlists(self) -> None:
        store = self.app.library.playlists
        self._playlists_repaint_source: int | None = None
        store.connect("items-changed", self._schedule_playlists_repaint)
        self.playlists_stack.set_visible_child_name("list")
        store.connect("items-changed", self._refresh_playlists_stack)

    # ------- empty-state stack toggles -------

    def _observe_windowed(self, store, stack: Gtk.Stack) -> None:
        def refresh(*_):
            stack.set_visible_child_name(
                "empty" if store.is_empty_after_load else "list"
            )
        store.connect("items-changed", refresh)
        store.connect("load-state-changed", refresh)
        refresh()

    def _refresh_playlists_stack(self, store, _pos, _removed, _added) -> None:
        empty = store.get_n_items() == 0
        self.playlists_stack.set_visible_child_name("empty" if empty else "list")

    def _schedule_playlists_repaint(self, store, _pos, _removed, _added) -> None:
        if self._playlists_repaint_source is not None:
            GLib.source_remove(self._playlists_repaint_source)

        def do_repaint():
            self._playlists_repaint_source = None
            self._repaint_playlists(store)
            return False

        self._playlists_repaint_source = GLib.idle_add(do_repaint)

    def _repaint_playlists(self, store) -> None:
        for child in list(self.playlists_list):
            self.playlists_list.remove(child)
        for i in range(store.get_n_items()):
            playlist = store.get_item(i).payload
            row = Adw.ActionRow(title=escape_markup(playlist.name), activatable=True)
            row.add_prefix(Gtk.Image.new_from_icon_name("view-list-symbolic"))
            row.add_suffix(Gtk.Image.new_from_icon_name("go-next-symbolic"))
            row.connect("activated", lambda _r, pl=playlist: self.window.open_playlist(pl))
            self.playlists_list.append(row)
