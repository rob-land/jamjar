"""Add a track to an existing playlist or create a new one."""

from __future__ import annotations

import logging
from gettext import gettext as _
from typing import TYPE_CHECKING

from gi.repository import Adw, GLib, Gtk

from ._common import escape_markup

if TYPE_CHECKING:
    from ..application import JamjarApplication
    from ..models import Playlist, Track
    from ..window import JamjarWindow

log = logging.getLogger(__name__)


class AddToPlaylistDialog(Adw.Dialog):
    __gtype_name__ = "JamjarAddToPlaylistDialog"

    def __init__(self, track: Track, app: JamjarApplication,
                 window: JamjarWindow) -> None:
        super().__init__()
        self.track = track
        self.app = app
        self.window = window
        self._playlists: list[Playlist] = []

        self.set_title(_("Add to Playlist"))
        self.set_content_width(400)
        self.set_content_height(480)

        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        header.set_show_end_title_buttons(False)
        toolbar.add_top_bar(header)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                      margin_top=12, margin_bottom=12,
                      margin_start=12, margin_end=12)
        self._status = Gtk.Label(xalign=0)
        self._status.add_css_class("dim-label")
        box.append(self._status)

        scrolled = Gtk.ScrolledWindow(vexpand=True)
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self._group = Adw.PreferencesGroup()
        scrolled.set_child(self._group)
        box.append(scrolled)

        create_row = Adw.ButtonRow(title=_("Create new playlist…"))
        create_row.add_prefix(Gtk.Image.new_from_icon_name("list-add-symbolic"))
        create_row.connect("activated", self._on_create_new)
        box.append(create_row)

        toolbar.set_content(box)
        self.set_child(toolbar)
        self._status.set_label(_("Loading playlists…"))
        GLib.idle_add(self._load_playlists)

    def _load_playlists(self) -> bool:
        if self.app.client is None:
            return False

        async def runme():
            return await self.app.client.list_playlists(limit=500)

        def done(future):
            try:
                playlists = future.result()
            except Exception as e:
                log.warning("playlist list failed: %s", e)
                playlists = []
                msg = _("Couldn't load playlists.")
                GLib.idle_add(lambda: (self._show_error(msg), False)[1])
                return
            GLib.idle_add(lambda: (self._populate(playlists), False)[1])

        self.app.runner.submit(runme()).add_done_callback(done)
        return False

    def _show_error(self, message: str) -> bool:
        self._status.set_label(message)
        for child in list(self._group):
            self._group.remove(child)
        return False

    def _populate(self, playlists: list[Playlist]) -> bool:
        self._playlists = playlists
        for child in list(self._group):
            self._group.remove(child)
        if not playlists:
            self._status.set_label(_("No playlists yet — create one below."))
            return False
        self._status.set_label(_("Choose a playlist"))
        for playlist in playlists:
            row = Adw.ActionRow(
                title=escape_markup(playlist.name),
                activatable=True,
            )
            if playlist.track_count is not None:
                row.set_subtitle(_("%d tracks") % playlist.track_count)
            row.add_prefix(Gtk.Image.new_from_icon_name("view-list-symbolic"))
            row.connect("activated", self._on_pick, playlist.id)
            self._group.add(row)
        return False

    def _on_pick(self, _row, playlist_id: str) -> None:
        self._add_to(playlist_id)

    def _on_create_new(self, _row) -> None:
        entry = Gtk.Entry()
        entry.set_placeholder_text(_("Playlist name"))
        dialog = Adw.AlertDialog(
            heading=_("New playlist"),
            body=_("Enter a name for the playlist."),
        )
        dialog.set_extra_child(entry)
        dialog.add_response("cancel", _("_Cancel"))
        dialog.add_response("create", _("_Create"))
        dialog.set_default_response("create")
        dialog.set_close_response("cancel")
        dialog.set_response_appearance("create", Adw.ResponseAppearance.SUGGESTED)

        def on_response(_dlg, response: str) -> None:
            if response != "create":
                return
            name = entry.get_text().strip()
            if not name:
                if self.app.show_toast:
                    self.app.show_toast(_("Enter a playlist name."))
                return
            self._create_and_add(name)

        dialog.connect("response", on_response)
        dialog.present(self.window)

    def _add_to(self, playlist_id: str) -> None:
        if self.app.client is None:
            return
        self._status.set_label(_("Adding…"))

        async def runme():
            await self.app.client.add_to_playlist(playlist_id, [self.track.id])

        def done(future):
            try:
                future.result()
            except Exception as e:
                log.warning("add to playlist failed: %s", e)
                msg = _("Couldn't add to playlist.")
                GLib.idle_add(lambda: (self._show_error(msg), False)[1])
                return
            if self.app.show_toast:
                self.app.show_toast(_("Added to playlist"))
            GLib.idle_add(lambda: (self.close(), False)[1])

        self.app.runner.submit(runme()).add_done_callback(done)

    def _create_and_add(self, name: str) -> None:
        if self.app.client is None:
            return
        self._status.set_label(_("Creating…"))

        async def runme():
            return await self.app.client.create_playlist(
                name, item_ids=[self.track.id],
            )

        def done(future):
            try:
                future.result()
            except Exception as e:
                log.warning("create playlist failed: %s", e)
                msg = _("Couldn't create playlist.")
                GLib.idle_add(lambda: (self._show_error(msg), False)[1])
                return
            if self.app.show_toast:
                self.app.show_toast(_("Playlist created"))
            GLib.idle_add(lambda: (self.close(), False)[1])

        self.app.runner.submit(runme()).add_done_callback(done)


def show_add_to_playlist_dialog(track: Track, app: JamjarApplication,
                               window: JamjarWindow) -> None:
    AddToPlaylistDialog(track, app, window).present(window)
