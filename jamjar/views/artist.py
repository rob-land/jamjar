"""Artist detail page."""

from __future__ import annotations

from typing import TYPE_CHECKING

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk

from ._common import (apply_favorite_visual, clear_remote_image,
                      commit_favorite, load_remote_image_async)
from .album_menu import install_album_menu

if TYPE_CHECKING:
    from ..application import JamjarApplication
    from ..window import JamjarWindow


@Gtk.Template(resource_path="/land/rob/Jamjar/artist-page.ui")
class ArtistPage(Adw.NavigationPage):
    __gtype_name__ = "JamjarArtistPage"

    artist_image        = Gtk.Template.Child()
    artist_name_label   = Gtk.Template.Child()
    artist_meta_label   = Gtk.Template.Child()
    favorite_button     = Gtk.Template.Child()
    artist_albums_grid  = Gtk.Template.Child()

    def __init__(self, app: "JamjarApplication", window: "JamjarWindow", artist) -> None:
        super().__init__()
        self.app = app
        self.window = window
        self.artist = artist

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

    def _on_albums_loaded(self, albums: list) -> None:
        from ..library import _Wrapper
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
