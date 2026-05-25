"""Home page — recently played / added / suggested rows."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from gi.repository import Adw, GLib, Gtk

from ._common import load_remote_image_async
from .album_menu import install_album_menu
from .track_menu import install_track_menu

if TYPE_CHECKING:
    from ..application import JamjarApplication
    from ..window import JamjarWindow

log = logging.getLogger(__name__)


@Gtk.Template(resource_path="/land/rob/jamjar/home-page.ui")
class HomePage(Adw.NavigationPage):
    __gtype_name__ = "JamjarHomePage"

    sidebar_toggle      = Gtk.Template.Child()
    home_refresh        = Gtk.Template.Child()
    recently_played_row = Gtk.Template.Child()
    recently_added_row  = Gtk.Template.Child()
    suggested_row       = Gtk.Template.Child()

    def __init__(self, app: JamjarApplication, window: JamjarWindow) -> None:
        super().__init__()
        self.app = app
        self.window = window
        self.sidebar_toggle.connect("clicked", lambda *_: self.window.toggle_sidebar())
        self.home_refresh.connect("clicked", self._on_refresh)
        self._pending_repaints: dict[str, int] = {}
        GLib.idle_add(self._refresh)

    def _on_refresh(self, _btn) -> None:
        if self.app.library is None:
            return
        self.app.library.refresh_all()
        if self.app.show_toast:
            self.app.show_toast("Refreshing…")

    def _refresh(self) -> bool:
        lib = self.app.library
        if lib is None:
            return False
        lib.load_recently_played()
        lib.load_recently_added()
        lib.load_suggested()
        lib.recently_played.connect("items-changed", self._on_recently_played_changed)
        lib.recently_added.connect("items-changed", self._on_recently_added_changed)
        lib.suggested.connect("items-changed", self._on_suggested_changed)
        return False

    def _on_recently_added_changed(self, store, _pos, _removed, _added) -> None:
        self._schedule_repaint("added", self.recently_added_row, store,
                               self._tile_for_album)

    def _on_recently_played_changed(self, store, _pos, _removed, _added) -> None:
        self._schedule_repaint("played", self.recently_played_row, store,
                               self._tile_for_track)

    def _on_suggested_changed(self, store, _pos, _removed, _added) -> None:
        self._schedule_repaint("suggested", self.suggested_row, store,
                               self._tile_for_track)

    def _schedule_repaint(self, key: str, row, store, tile_builder) -> None:
        if key in self._pending_repaints:
            GLib.source_remove(self._pending_repaints[key])

        def do_repaint():
            del self._pending_repaints[key]
            self._repaint_row(row, store, tile_builder)
            return False

        self._pending_repaints[key] = GLib.idle_add(do_repaint)

    def _repaint_row(self, row: Gtk.Box, store, tile_builder) -> None:
        for child in list(row):
            row.remove(child)
        for i in range(store.get_n_items()):
            payload = store.get_item(i).payload
            row.append(tile_builder(payload))

    def _tile_for_album(self, album) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        picture = Gtk.Picture(content_fit=Gtk.ContentFit.COVER,
                              width_request=128, height_request=128)
        picture.add_css_class("cover")
        if album.image_tag:
            url = self.app.client.cover_url(album.id, album.image_tag,
                                            max_width=256)
            load_remote_image_async(url, self.app.client.headers, picture,
                                    self.app.client.session, self.app.runner)
        box.append(picture)
        title = Gtk.Label(label=album.name, ellipsize=3, xalign=0)
        title.add_css_class("heading")
        box.append(title)
        artist = Gtk.Label(label=album.primary_artist, ellipsize=3, xalign=0)
        artist.add_css_class("dim-label")
        artist.add_css_class("caption")
        box.append(artist)

        # valign=START so the button doesn't stretch to the row's height
        # (which would make the hover highlight cover empty space below).
        button = Gtk.Button(width_request=140, child=box,
                            valign=Gtk.Align.START)
        button.add_css_class("flat")
        button.connect("clicked", lambda *_: self.window.open_album(album))
        install_album_menu(button, lambda al=album: al, self.app, self.window)
        return button

    def _tile_for_track(self, track) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        picture = Gtk.Picture(content_fit=Gtk.ContentFit.COVER,
                              width_request=128, height_request=128)
        picture.add_css_class("cover")
        # Prefer the album's cover (most tracks share it) and fall back to
        # any track-level art.
        image_id = track.album_id or track.id
        image_tag = track.album_image_tag or track.image_tag
        if image_tag:
            url = self.app.client.cover_url(image_id, image_tag, max_width=256)
            load_remote_image_async(url, self.app.client.headers, picture,
                                    self.app.client.session, self.app.runner)
        box.append(picture)
        title = Gtk.Label(label=track.name, ellipsize=3, xalign=0)
        title.add_css_class("heading")
        box.append(title)
        artist = Gtk.Label(label=track.primary_artist, ellipsize=3, xalign=0)
        artist.add_css_class("dim-label")
        artist.add_css_class("caption")
        box.append(artist)
        button = Gtk.Button(width_request=140, child=box, valign=Gtk.Align.START)
        button.add_css_class("flat")
        button.connect("clicked", lambda *_: self._play_track(track))
        install_track_menu(button, lambda t=track: t, self.app, self.window)
        return button

    def _play_track(self, track) -> None:
        if self.app.queue is None or self.app.player is None:
            return
        self.app.queue.replace([track], start_index=0)
        self.app.player.play(track)
