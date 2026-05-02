"""Album detail page."""

from __future__ import annotations

from typing import TYPE_CHECKING

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk

from ._common import (apply_favorite_visual, commit_favorite, escape_markup,
                       format_duration, load_remote_image_async)
from .track_menu import install_track_menu

if TYPE_CHECKING:
    from ..application import JamjarApplication
    from ..window import JamjarWindow


@Gtk.Template(resource_path="/land/rob/Jamjar/album-page.ui")
class AlbumPage(Adw.NavigationPage):
    __gtype_name__ = "JamjarAlbumPage"

    cover_picture       = Gtk.Template.Child()
    album_title_label   = Gtk.Template.Child()
    album_artist_label  = Gtk.Template.Child()
    album_meta_label    = Gtk.Template.Child()
    play_button         = Gtk.Template.Child()
    shuffle_button      = Gtk.Template.Child()
    favorite_button     = Gtk.Template.Child()
    tracks_list         = Gtk.Template.Child()

    def __init__(self, app: "JamjarApplication", window: "JamjarWindow", album) -> None:
        super().__init__()
        self.app = app
        self.window = window
        self.album = album
        self.tracks: list = []

        self.set_title(album.name)
        self.album_title_label.set_label(album.name)
        self.album_artist_label.set_label(album.primary_artist)
        meta_bits = []
        if album.year:
            meta_bits.append(str(album.year))
        if album.track_count:
            meta_bits.append(f"{album.track_count} tracks")
        self.album_meta_label.set_label(" • ".join(meta_bits))

        if album.image_tag:
            url = app.client.cover_url(album.id, album.image_tag, max_width=512)
            load_remote_image_async(url, app.client.headers, self.cover_picture,
                                    app.client.session, app.runner)

        self.play_button.connect("clicked", self._on_play)
        self.shuffle_button.connect("clicked", self._on_shuffle)

        self._suppress_favorite = False
        self._sync_favorite(bool(album.user_data.get("IsFavorite")))
        self.favorite_button.connect("toggled", self._on_favorite_toggled)

        app.library.album_tracks(album.id, self._on_tracks_loaded)

    def _sync_favorite(self, is_favorite: bool) -> None:
        self._suppress_favorite = True
        self.favorite_button.set_active(is_favorite)
        self._suppress_favorite = False
        apply_favorite_visual(self.favorite_button, is_favorite)

    def _on_favorite_toggled(self, button) -> None:
        if self._suppress_favorite or self.app.client is None:
            return
        new_state = button.get_active()
        apply_favorite_visual(button, new_state)
        commit_favorite(self.app.client, self.album, new_state, self.app.runner,
                        on_failure=lambda: self._sync_favorite(not new_state))

    def _on_tracks_loaded(self, tracks: list) -> None:
        self.tracks = tracks
        for child in list(self.tracks_list):
            self.tracks_list.remove(child)
        for index, track in enumerate(tracks):
            row = Adw.ActionRow(
                title=escape_markup(track.name),
                subtitle=escape_markup(track.primary_artist),
                activatable=True,
            )
            num_label = Gtk.Label(label=str(track.index_number or index + 1),
                                  width_chars=3)
            num_label.add_css_class("dim-label")
            row.add_prefix(num_label)
            duration = Gtk.Label(label=format_duration(track.duration_seconds))
            duration.add_css_class("dim-label")
            row.add_suffix(duration)
            row.connect("activated", lambda _r, i=index: self._play_from(i))
            install_track_menu(row, lambda t=track: t, self.app, self.window)
            self.tracks_list.append(row)

    def _play_from(self, index: int) -> None:
        self.app.queue.replace(self.tracks, start_index=index)
        self.app.player.play(self.tracks[index])

    def _on_play(self, _btn) -> None:
        if self.tracks:
            self._play_from(0)

    def _on_shuffle(self, _btn) -> None:
        if not self.tracks:
            return
        self.app.queue.shuffle = True
        self.app.queue.replace(self.tracks, start_index=0)
        self.app.player.play(self.app.queue.current)
