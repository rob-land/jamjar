"""Playlist detail page."""

from __future__ import annotations

from typing import TYPE_CHECKING

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, Gtk

from ._common import format_duration
from .track_menu import install_track_menu

if TYPE_CHECKING:
    from ..application import JamjarApplication
    from ..window import JamjarWindow


@Gtk.Template(resource_path="/land/rob/Jamjar/playlist-page.ui")
class PlaylistPage(Adw.NavigationPage):
    __gtype_name__ = "JamjarPlaylistPage"

    rename_button         = Gtk.Template.Child()
    delete_button         = Gtk.Template.Child()
    playlist_title_label  = Gtk.Template.Child()
    playlist_meta_label   = Gtk.Template.Child()
    playlist_tracks       = Gtk.Template.Child()

    def __init__(self, app: "JamjarApplication", window: "JamjarWindow", playlist) -> None:
        super().__init__()
        self.app = app
        self.window = window
        self.playlist = playlist
        self.tracks: list = []

        self.set_title(playlist.name)
        self.playlist_title_label.set_label(playlist.name)
        if playlist.track_count:
            self.playlist_meta_label.set_label(f"{playlist.track_count} tracks")

        from ..library import _Wrapper
        self.store = Gio.ListStore.new(_Wrapper)
        factory = Gtk.SignalListItemFactory()
        factory.connect("setup", self._row_setup)
        factory.connect("bind",  self._row_bind)
        self.playlist_tracks.set_model(Gtk.NoSelection.new(self.store))
        self.playlist_tracks.set_factory(factory)
        self.playlist_tracks.connect("activate", self._row_activated)

        app.runner.submit(self._load())

    async def _load(self) -> None:
        from gi.repository import GLib
        tracks = await self.app.client.playlist_tracks(self.playlist.id)
        from ..library import _Wrapper

        def apply():
            self.tracks = tracks
            self.store.remove_all()
            for t in tracks:
                self.store.append(_Wrapper(t))
            return False
        GLib.idle_add(apply)

    def _row_setup(self, _factory, item) -> None:
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12,
                      margin_top=6, margin_bottom=6, margin_start=12, margin_end=12)
        title = Gtk.Label(xalign=0, ellipsize=3, hexpand=True)
        title.add_css_class("heading")
        artist = Gtk.Label(xalign=0, ellipsize=3, hexpand=False, width_chars=20)
        artist.add_css_class("dim-label")
        duration = Gtk.Label(xalign=1)
        duration.add_css_class("dim-label")
        box.append(title)
        box.append(artist)
        box.append(duration)
        item.set_child(box)
        item.title_label, item.artist_label, item.duration_label = title, artist, duration
        install_track_menu(box,
                           lambda it=item: (it.get_item().payload
                                            if it.get_item() else None),
                           self.app, self.window)

    def _row_bind(self, _factory, item) -> None:
        track = item.get_item().payload
        item.title_label.set_label(track.name)
        item.artist_label.set_label(track.primary_artist)
        item.duration_label.set_label(format_duration(track.duration_seconds))

    def _row_activated(self, _view, position) -> None:
        if not self.tracks or position >= len(self.tracks):
            return
        self.app.queue.replace(self.tracks, start_index=position)
        self.app.player.play(self.tracks[position])
