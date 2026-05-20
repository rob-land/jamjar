"""Search page with debounced search-hint queries."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from gi.repository import Adw, Gdk, GLib, Gtk

from ..models import album_from_json, artist_from_json, track_from_json
from ._common import escape_markup
from .track_menu import show_track_popover

if TYPE_CHECKING:
    from ..application import JamjarApplication
    from ..window import JamjarWindow

log = logging.getLogger(__name__)

DEBOUNCE_MS = 250


@Gtk.Template(resource_path="/land/rob/jamjar/search-page.ui")
class SearchPage(Adw.NavigationPage):
    __gtype_name__ = "JamjarSearchPage"

    sidebar_toggle = Gtk.Template.Child()
    search_bar     = Gtk.Template.Child()
    search_entry   = Gtk.Template.Child()
    tracks_group   = Gtk.Template.Child()
    albums_group   = Gtk.Template.Child()
    artists_group  = Gtk.Template.Child()
    empty_state    = Gtk.Template.Child()

    def __init__(self, app: JamjarApplication, window: JamjarWindow) -> None:
        super().__init__()
        self.app = app
        self.window = window
        self.sidebar_toggle.connect("clicked", lambda *_: self.window.toggle_sidebar())
        self._debounce_source = None
        self._search_seq = 0
        self._rows_by_group: dict[Adw.PreferencesGroup, list[Adw.ActionRow]] = {
            self.tracks_group:  [],
            self.albums_group:  [],
            self.artists_group: [],
        }
        self.search_entry.connect("search-changed", self._on_changed)

    def focus_entry(self) -> None:
        self.search_bar.set_search_mode(True)
        self.search_entry.grab_focus()

    def _on_changed(self, entry) -> None:
        if self._debounce_source is not None:
            GLib.source_remove(self._debounce_source)
            self._debounce_source = None
        text = entry.get_text().strip()
        if not text:
            # Bump the seq so any in-flight callback is ignored.
            self._search_seq += 1
            self._show_empty()
            return
        self._debounce_source = GLib.timeout_add(DEBOUNCE_MS, lambda: self._fire(text))

    def _fire(self, text: str) -> bool:
        self._debounce_source = None
        if self.app.library is None:
            return False
        self._search_seq += 1
        seq = self._search_seq
        self.app.library.search(text, lambda hits: self._render(seq, hits))
        return False

    def _show_empty(self) -> None:
        self._clear_rows()
        self.empty_state.set_visible(True)
        self.tracks_group.set_visible(False)
        self.albums_group.set_visible(False)
        self.artists_group.set_visible(False)

    def _clear_rows(self) -> None:
        for group, rows in self._rows_by_group.items():
            for row in rows:
                group.remove(row)
            rows.clear()

    def _render(self, seq: int, hits) -> None:
        # Drop stale callbacks: a faster, later request must not be
        # overwritten by a slower, earlier one.
        if seq != self._search_seq:
            return
        self._clear_rows()
        self.empty_state.set_visible(False)

        any_tracks = any_albums = any_artists = False
        for hit in hits:
            row = Adw.ActionRow(title=escape_markup(hit.name),
                                subtitle=escape_markup(hit.secondary),
                                activatable=True)
            row.add_prefix(Gtk.Image.new_from_icon_name(_icon_for_type(hit.type)))
            row.connect("activated", self._on_row_activated, hit)
            if hit.type == "Audio":
                self._install_audio_row_menu(row, hit)
                self.tracks_group.add(row)
                self._rows_by_group[self.tracks_group].append(row)
                any_tracks = True
            elif hit.type == "MusicAlbum":
                self.albums_group.add(row)
                self._rows_by_group[self.albums_group].append(row)
                any_albums = True
            elif hit.type == "MusicArtist":
                self.artists_group.add(row)
                self._rows_by_group[self.artists_group].append(row)
                any_artists = True

        self.tracks_group.set_visible(any_tracks)
        self.albums_group.set_visible(any_albums)
        self.artists_group.set_visible(any_artists)

    def _install_audio_row_menu(self, row: Adw.ActionRow, hit) -> None:
        """Right-click / long-press on an Audio search row pops the standard
        track menu. The Track isn't in hand at gesture time (search returns
        SearchHit, which only carries an item id), so we fetch it on first
        request and cache it on the row for repeat right-clicks.
        """
        def show_at(x: float, y: float) -> None:
            cached = getattr(row, "_jamjar_track", None)
            if cached is not None:
                show_track_popover(cached, self.app, self.window, row, x, y)
                return
            if self.app.client is None:
                return

            async def fetch():
                return await self.app.client.get_item(hit.item_id)

            def done(future):
                try:
                    item = future.result()
                except Exception as e:
                    log.warning("track fetch for menu failed: %s", e)
                    return
                track = track_from_json(item)
                row._jamjar_track = track
                GLib.idle_add(lambda: (show_track_popover(track, self.app,
                                                          self.window, row,
                                                          x, y), False)[1])

            self.app.runner.submit(fetch()).add_done_callback(done)

        rc = Gtk.GestureClick.new()
        rc.set_button(Gdk.BUTTON_SECONDARY)
        rc.connect("pressed", lambda _g, _n, x, y: show_at(x, y))
        row.add_controller(rc)

        lp = Gtk.GestureLongPress.new()
        lp.connect("pressed", lambda _g, x, y: show_at(x, y))
        row.add_controller(lp)

    def _on_row_activated(self, _row, hit) -> None:
        if self.app.client is None:
            return

        async def fetch():
            return await self.app.client.get_item(hit.item_id)

        def done(future):
            try:
                item = future.result()
            except Exception as e:
                log.warning("failed to fetch search hit %s: %s", hit.item_id, e)
                return
            GLib.idle_add(lambda: (self._dispatch(hit.type, item), False)[1])

        self.app.runner.submit(fetch()).add_done_callback(done)

    def _dispatch(self, type_: str, item: dict) -> None:
        if type_ == "Audio":
            track = track_from_json(item)
            self.app.queue.replace([track], start_index=0)
            self.app.player.play(track)
        elif type_ == "MusicAlbum":
            self.window.open_album(album_from_json(item))
        elif type_ == "MusicArtist":
            self.window.open_artist(artist_from_json(item))


def _icon_for_type(type_: str) -> str:
    return {
        "Audio":       "audio-x-generic-symbolic",
        "MusicAlbum":  "media-optical-cd-audio-symbolic",
        "MusicArtist": "system-users-symbolic",
    }.get(type_, "edit-find-symbolic")
