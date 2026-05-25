"""Playlist detail page — view, reorder, rename, and delete."""

from __future__ import annotations

import logging
from dataclasses import replace
from gettext import gettext as _, ngettext
from typing import TYPE_CHECKING

from gi.repository import Adw, Gdk, GLib, GObject, Gtk

from ._common import escape_markup, favorite_heart, format_duration
from .track_menu import install_track_menu

if TYPE_CHECKING:
    from ..application import JamjarApplication
    from ..models import Playlist, Track
    from ..window import JamjarWindow

log = logging.getLogger(__name__)


@Gtk.Template(resource_path="/land/rob/jamjar/playlist-page.ui")
class PlaylistPage(Adw.NavigationPage):
    __gtype_name__ = "JamjarPlaylistPage"

    rename_button         = Gtk.Template.Child()
    delete_button         = Gtk.Template.Child()
    play_button           = Gtk.Template.Child()
    shuffle_button        = Gtk.Template.Child()
    playlist_title_label  = Gtk.Template.Child()
    playlist_meta_label   = Gtk.Template.Child()
    playlist_tracks       = Gtk.Template.Child()

    def __init__(self, app: JamjarApplication, window: JamjarWindow,
                 playlist: Playlist) -> None:
        super().__init__()
        self.app = app
        self.window = window
        self.playlist = playlist
        self.tracks: list[Track] = []
        self._row_hearts: dict[str, list[Gtk.Image]] = {}

        self.set_title(playlist.name)
        self._update_header()

        self.rename_button.connect("clicked", self._on_rename)
        self.delete_button.connect("clicked", self._on_delete)
        self.play_button.connect("clicked", self._on_play)
        self.shuffle_button.connect("clicked", self._on_shuffle)
        self._fav_handler = app.connect("favorite-changed", self._on_favorite_changed_external)
        self.connect("unrealize", self._on_unrealize)

        app.runner.submit(self._load())

    def _update_header(self) -> None:
        self.playlist_title_label.set_label(self.playlist.name)
        n = len(self.tracks) if self.tracks else (self.playlist.track_count or 0)
        if n:
            self.playlist_meta_label.set_label(
                ngettext("%d track", "%d tracks", n) % n
            )
        else:
            self.playlist_meta_label.set_label("")

    async def _load(self) -> None:
        tracks = await self.app.client.playlist_tracks(self.playlist.id)

        def apply():
            self.tracks = tracks
            self._update_header()
            self._refresh_rows()
            return False

        GLib.idle_add(apply)

    def _refresh_rows(self) -> None:
        for child in list(self.playlist_tracks):
            self.playlist_tracks.remove(child)
        self._row_hearts.clear()

        for index, track in enumerate(self.tracks):
            row = Adw.ActionRow(
                title=escape_markup(track.name),
                subtitle=escape_markup(track.primary_artist),
                activatable=True,
            )

            handle = Gtk.Image.new_from_icon_name("list-drag-handle-symbolic")
            handle.add_css_class("dim-label")
            handle.set_valign(Gtk.Align.CENTER)
            handle.set_tooltip_text(_("Drag to reorder"))
            self._install_drag_source(handle, index)
            row.add_prefix(handle)

            num = Gtk.Label(label=str(index + 1), width_chars=3)
            num.add_css_class("dim-label")
            num.add_css_class("numeric")
            row.add_prefix(num)

            heart = favorite_heart(bool(track.user_data.get("IsFavorite")))
            row.add_suffix(heart)
            self._row_hearts.setdefault(track.id, []).append(heart)

            duration = Gtk.Label(label=format_duration(track.duration_seconds))
            duration.add_css_class("dim-label")
            duration.add_css_class("numeric")
            row.add_suffix(duration)

            remove_btn = Gtk.Button.new_from_icon_name("user-trash-symbolic")
            remove_btn.add_css_class("flat")
            remove_btn.set_tooltip_text(_("Remove from playlist"))
            remove_btn.set_valign(Gtk.Align.CENTER)
            remove_btn.connect("clicked", lambda _b, i=index: self._remove_at(i))
            row.add_suffix(remove_btn)

            row.connect("activated", lambda _r, i=index: self._play_from(i))
            install_track_menu(row, lambda t=track: t, self.app, self.window)
            self._install_drop_target(row, index)
            self.playlist_tracks.append(row)

    def _install_drag_source(self, widget: Gtk.Widget, index: int) -> None:
        src = Gtk.DragSource.new()
        src.set_actions(Gdk.DragAction.MOVE)

        def on_prepare(_src, _x, _y):
            value = GObject.Value()
            value.init(GObject.TYPE_INT)
            value.set_int(index)
            return Gdk.ContentProvider.new_for_value(value)

        src.connect("prepare", on_prepare)
        widget.add_controller(src)

    def _install_drop_target(self, row: Gtk.Widget, target_index: int) -> None:
        target = Gtk.DropTarget.new(int, Gdk.DragAction.MOVE)

        def on_drop(_t, value, _x, _y):
            try:
                src_index = int(value)
            except (TypeError, ValueError):
                return False
            if src_index == target_index:
                return False
            self._move_track(src_index, target_index)
            return True

        target.connect("drop", on_drop)
        row.add_controller(target)

    def _move_track(self, src_index: int, dest_index: int) -> None:
        if src_index < 0 or src_index >= len(self.tracks):
            return
        if dest_index < 0 or dest_index >= len(self.tracks):
            return
        track = self.tracks.pop(src_index)
        self.tracks.insert(dest_index, track)
        self._refresh_rows()

        async def runme():
            await self.app.client.move_playlist_item(
                self.playlist.id, track.id, dest_index,
            )

        def done(future):
            try:
                future.result()
            except Exception as e:
                log.warning("playlist move failed: %s", e)
                GLib.idle_add(lambda: (self._reload_after_error(), False)[1])

        self.app.runner.submit(runme()).add_done_callback(done)

    def _reload_after_error(self) -> bool:
        if self.app.show_toast:
            self.app.show_toast(_("Couldn't reorder playlist."))
        self.app.runner.submit(self._load())
        return False

    def _remove_at(self, index: int) -> None:
        if index < 0 or index >= len(self.tracks):
            return
        track = self.tracks[index]
        self.tracks.pop(index)
        self._update_header()
        self._refresh_rows()

        async def runme():
            await self.app.client.remove_from_playlist(
                self.playlist.id, [track.id],
            )

        def done(future):
            try:
                future.result()
            except Exception as e:
                log.warning("playlist remove failed: %s", e)
                GLib.idle_add(lambda: (self._reload_after_error(), False)[1])

        self.app.runner.submit(runme()).add_done_callback(done)

    def _play_from(self, index: int) -> None:
        if not self.tracks or index >= len(self.tracks):
            return
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

    def _on_unrealize(self, _widget) -> None:
        self.app.disconnect(self._fav_handler)

    def _on_favorite_changed_external(self, _app, item_id: str,
                                    is_favorite: bool) -> None:
        for heart in self._row_hearts.get(item_id, ()):
            heart.set_visible(is_favorite)

    def _on_rename(self, _btn) -> None:
        entry = Gtk.Entry(text=self.playlist.name)
        dialog = Adw.AlertDialog(
            heading=_("Rename playlist"),
            body=_("Enter a new name for this playlist."),
        )
        dialog.set_extra_child(entry)
        dialog.add_response("cancel", _("_Cancel"))
        dialog.add_response("save", _("_Save"))
        dialog.set_default_response("save")
        dialog.set_close_response("cancel")
        dialog.set_response_appearance("save", Adw.ResponseAppearance.SUGGESTED)

        def on_response(_dlg, response: str) -> None:
            if response != "save":
                return
            name = entry.get_text().strip()
            if not name or name == self.playlist.name:
                return
            self._save_rename(name)

        dialog.connect("response", on_response)
        dialog.present(self.window)

    def _save_rename(self, name: str) -> None:
        old_name = self.playlist.name
        self.playlist = replace(self.playlist, name=name)
        self.set_title(name)
        self._update_header()

        async def runme():
            await self.app.client.update_playlist(self.playlist.id, name=name)

        def done(future):
            try:
                future.result()
            except Exception as e:
                log.warning("playlist rename failed: %s", e)

                def rollback() -> bool:
                    self.playlist = replace(self.playlist, name=old_name)
                    self.set_title(old_name)
                    self._update_header()
                    if self.app.show_toast:
                        self.app.show_toast(_("Couldn't rename playlist."))
                    return False

                GLib.idle_add(rollback)
                return
            if self.app.library:
                self.app.library.refresh_playlists()
            if self.app.show_toast:
                self.app.show_toast(_("Playlist renamed"))

        self.app.runner.submit(runme()).add_done_callback(done)

    def _on_delete(self, _btn) -> None:
        dialog = Adw.AlertDialog(
            heading=_("Delete playlist?"),
            body=_('Delete “%s”? This cannot be undone.') % GLib.markup_escape_text(self.playlist.name),
        )
        dialog.add_response("cancel", _("_Cancel"))
        dialog.add_response("delete", _("_Delete"))
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)

        def on_response(_dlg, response: str) -> None:
            if response != "delete":
                return
            self._confirm_delete()

        dialog.connect("response", on_response)
        dialog.present(self.window)

    def _confirm_delete(self) -> None:
        playlist_id = self.playlist.id

        async def runme():
            await self.app.client.delete_playlist(playlist_id)

        def done(future):
            try:
                future.result()
            except Exception as e:
                log.warning("playlist delete failed: %s", e)
                if self.app.show_toast:
                    self.app.show_toast(_("Couldn't delete playlist."))
                return
            if self.app.library:
                self.app.library.refresh_playlists()
            if self.app.show_toast:
                self.app.show_toast(_("Playlist deleted"))
            GLib.idle_add(self._pop_after_delete)

        self.app.runner.submit(runme()).add_done_callback(done)

    def _pop_after_delete(self) -> bool:
        nav = self.window.nav_view
        pages = nav.get_pages()
        if pages.get_n_items() > 1:
            nav.pop()
        return False
