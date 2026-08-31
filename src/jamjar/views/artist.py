"""Artist detail page."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from gi.repository import Adw, GLib, Gtk

from ..models import Artist
from ._common import (
    apply_favorite_visual,
    artist_tile,
    escape_markup,
    format_duration,
    clear_remote_image,
    commit_favorite,
    load_remote_image_async,
    start_instant_mix,
)
from .album_menu import install_album_menu
from .track_menu import install_track_menu

if TYPE_CHECKING:
    from ..application import JamjarApplication
    from ..window import JamjarWindow

log = logging.getLogger(__name__)


@Gtk.Template(resource_path="/land/rob/jamjar/artist-page.ui")
class ArtistPage(Adw.NavigationPage):
    __gtype_name__ = "JamjarArtistPage"

    artist_image        = Gtk.Template.Child()
    artist_name_label   = Gtk.Template.Child()
    artist_meta_label   = Gtk.Template.Child()
    artist_radio_button = Gtk.Template.Child()
    favorite_button     = Gtk.Template.Child()
    albums_spinner      = Gtk.Template.Child()
    artist_albums_grid  = Gtk.Template.Child()
    top_tracks_section  = Gtk.Template.Child()
    top_tracks_list     = Gtk.Template.Child()
    similar_section     = Gtk.Template.Child()
    similar_row         = Gtk.Template.Child()

    def __init__(self, app: JamjarApplication, window: JamjarWindow, artist) -> None:
        super().__init__()
        self.app = app
        self.window = window
        self.artist = artist
        self._top_tracks: list = []

        self.set_title(artist.name)
        self.artist_name_label.set_label(artist.name)
        if artist.album_count:
            self.artist_meta_label.set_label(f"{artist.album_count} albums")

        if artist.image_tag:
            url = app.client.cover_url(artist.id, artist.image_tag, max_width=512)
            load_remote_image_async(url, app.client.headers, self.artist_image,
                                    app.client.session, app.runner)

        self._suppress_favorite = False
        self._sync_favorite(bool(artist.user_data.get("IsFavorite")))
        self.artist_radio_button.connect("clicked", self._on_start_radio)
        self.favorite_button.connect("toggled", self._on_favorite_toggled)

        from gi.repository import Gio
        store = Gio.ListStore.new(self.app.library.albums.get_item_type())
        self.store = store
        factory = Gtk.SignalListItemFactory()
        factory.connect("setup", self._tile_setup)
        factory.connect("bind", self._tile_bind)
        self.artist_albums_grid.set_model(Gtk.NoSelection.new(store))
        self.artist_albums_grid.set_factory(factory)
        self.artist_albums_grid.connect("activate", self._activated)

        app.library.artist_albums(artist.id, self._on_albums_loaded)
        self._load_similar()
        self._load_top_tracks()

    def _on_albums_loaded(self, albums: list) -> None:
        from ..library import _Wrapper
        self.albums_spinner.set_visible(False)
        self.artist_albums_grid.set_visible(True)
        self.store.remove_all()
        for album in albums:
            self.store.append(_Wrapper(album))

    def _tile_setup(self, _factory, item) -> None:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6,
                      margin_top=4, margin_bottom=4, margin_start=4, margin_end=4)
        picture = Gtk.Picture(content_fit=Gtk.ContentFit.COVER,
                              width_request=144, height_request=144)
        picture.add_css_class("cover")
        label = Gtk.Label(xalign=0, ellipsize=3)
        label.add_css_class("heading")
        box.append(picture)
        box.append(label)
        item.set_child(box)
        item.picture, item.label = picture, label
        install_album_menu(box,
                           lambda it=item: (it.get_item().payload
                                             if it.get_item() else None),
                           self.app, self.window)

    def _tile_bind(self, _factory, item) -> None:
        album = item.get_item().payload
        item.label.set_label(album.name)
        if album.image_tag:
            url = self.app.client.cover_url(album.id, album.image_tag, max_width=256)
            load_remote_image_async(url, self.app.client.headers, item.picture,
                                    self.app.client.session, self.app.runner)
        else:
            clear_remote_image(item.picture)

    def _activated(self, grid, position) -> None:
        model = grid.get_model()
        wrapper = model.get_item(position) if model else None
        if wrapper is None:
            return
        self.window.open_album(wrapper.payload)

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
        commit_favorite(self.app.client, self.artist, new_state, self.app.runner, app=self.app,
                        on_failure=lambda: self._sync_favorite(not new_state))

    def _on_start_radio(self, _button) -> None:
        start_instant_mix(self.artist, self.app, kind="artist")

    def _load_similar(self) -> None:
        """Fill the "Similar Artists" shelf. Best-effort: a server without
        the data (or an artist it can't match) leaves the shelf hidden."""
        if self.app.client is None:
            return
        client = self.app.client
        seed_id = self.artist.id

        async def runme():
            return await client.similar_items(seed_id, limit=12)

        def done(future):
            try:
                items = future.result()
            except Exception as e:
                log.info("similar artists unavailable for %s: %s", seed_id, e)
                return
            GLib.idle_add(self._apply_similar, items)

        self.app.runner.submit(runme()).add_done_callback(done)

    def _apply_similar(self, items) -> bool:
        matching = [i for i in items if isinstance(i, Artist)]
        if not matching:
            return False
        for child in list(self.similar_row):
            self.similar_row.remove(child)
        for item in matching:
            self.similar_row.append(artist_tile(item, self.app, self.window))
        self.similar_section.set_visible(True)
        return False

    def _load_top_tracks(self) -> None:
        if self.app.client is None:
            return
        client = self.app.client
        artist_id = self.artist.id

        async def runme():
            return await client.artist_top_tracks(artist_id)

        def done(future):
            try:
                tracks = future.result()
            except Exception as e:
                log.info("top tracks unavailable for %s: %s", artist_id, e)
                return
            GLib.idle_add(self._apply_top_tracks, tracks)

        self.app.runner.submit(runme()).add_done_callback(done)

    def _apply_top_tracks(self, tracks) -> bool:
        if not tracks:
            return False
        self._top_tracks = tracks
        for child in list(self.top_tracks_list):
            self.top_tracks_list.remove(child)
        for index, track in enumerate(tracks):
            row = Adw.ActionRow(title=escape_markup(track.name),
                                subtitle=escape_markup(track.album),
                                activatable=True)
            number = Gtk.Label(label=str(index + 1), width_chars=2)
            number.add_css_class("dim-label")
            number.add_css_class("numeric")
            row.add_prefix(number)
            plays = int(track.user_data.get("PlayCount") or 0)
            if plays:
                label = Gtk.Label(label=f"{plays} plays" if plays > 1 else "1 play")
                label.add_css_class("dim-label")
                label.add_css_class("caption")
                row.add_suffix(label)
            duration = Gtk.Label(label=format_duration(track.duration_seconds))
            duration.add_css_class("dim-label")
            duration.add_css_class("numeric")
            row.add_suffix(duration)
            row.connect("activated", lambda _r, i=index: self._play_top_track(i))
            install_track_menu(row, lambda t=track: t, self.app, self.window)
            self.top_tracks_list.append(row)
        self.top_tracks_section.set_visible(True)
        return False

    def _play_top_track(self, index: int) -> None:
        if self.app.queue is None:
            return
        self.app.queue.replace(self._top_tracks, start_index=index)
