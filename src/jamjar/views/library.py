"""Library tab — Albums / Artists / Songs / Playlists."""

from __future__ import annotations

import logging
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


@Gtk.Template(resource_path="/land/rob/jamjar/library-page.ui")
class LibraryPage(Adw.NavigationPage):
    __gtype_name__ = "JamjarLibraryPage"

    LETTER_TABS = ("albums", "artists", "songs")

    sidebar_toggle  = Gtk.Template.Child()
    stack           = Gtk.Template.Child()
    letter_button   = Gtk.Template.Child()
    albums_grid     = Gtk.Template.Child()
    artists_grid    = Gtk.Template.Child()
    songs_column    = Gtk.Template.Child()
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
        self.sidebar_toggle.connect("clicked", lambda *_: self.window.toggle_sidebar())
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
        model = {
            "albums":  self.app.library.albums,
            "artists": self.app.library.artists,
            "songs":   self.app.library.songs,
        }[name]

        if choice == "All":
            model.set_filter()
            self._tab_letters[name] = None
        elif choice == "#":
            model.set_filter(name_less_than="A")
            self._tab_letters[name] = "#"
        else:
            model.set_filter(name_starts_with=choice)
            self._tab_letters[name] = choice

        # set_filter resets and refetches; mark the tab as loaded so the
        # tab-change loader doesn't kick a redundant ensure_first_page.
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
        self.songs_column.set_model(selection)
        self._observe_windowed(store, self.songs_stack)

        for title, attr in (("Title", "name"), ("Artist", "primary_artist"),
                            ("Album", "album"), ("Duration", "duration_seconds")):
            factory = Gtk.SignalListItemFactory()

            def _setup(_f, it):
                label = Gtk.Label(xalign=0, ellipsize=3, margin_end=8)
                it.set_child(label)
                install_track_menu(label,
                                   lambda it=it: (it.get_item().payload
                                                   if it.get_item() else None),
                                   self.app, self.window)
            factory.connect("setup", _setup)

            def _bind(_f, it, attr=attr):
                track = it.get_item().payload
                value = getattr(track, attr, "")
                if attr == "duration_seconds":
                    value = format_duration(value)
                it.get_child().set_label(str(value))
                # Trigger paging from the first column only — bind fires for
                # every cell of every row, but request_more is idempotent and
                # checking once per row keeps the math obvious.
                if attr == "name":
                    self.app.library.songs.maybe_request_more(it.get_position())
            factory.connect("bind", _bind)

            column = Gtk.ColumnViewColumn.new(title, factory)
            column.set_expand(True if attr in ("name", "album") else False)
            self.songs_column.append_column(column)

        self.songs_column.connect("activate", self._song_activated)

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
        store.connect("items-changed", self._on_playlists_changed)
        # Plain Gio.ListStore doesn't have a load-state signal. Default to
        # "list" during the initial cold-load gap so the empty state isn't
        # flashed; flip on the first items-changed (which lands even when
        # the response is empty, since `_replace` always removes-all first).
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

    def _on_playlists_changed(self, store, _pos, _removed, _added) -> None:
        for child in list(self.playlists_list):
            self.playlists_list.remove(child)
        for i in range(store.get_n_items()):
            playlist = store.get_item(i).payload
            row = Adw.ActionRow(title=escape_markup(playlist.name), activatable=True)
            row.add_prefix(Gtk.Image.new_from_icon_name("view-list-symbolic"))
            row.add_suffix(Gtk.Image.new_from_icon_name("go-next-symbolic"))
            row.connect("activated", lambda _r, pl=playlist: self.window.open_playlist(pl))
            self.playlists_list.append(row)
