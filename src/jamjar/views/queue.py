"""Queue page — the upcoming track list with jump / clear / reorder controls."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from gi.repository import Adw, Gdk, GLib, GObject, Gtk

from ._common import escape_markup, favorite_heart, format_duration
from .track_menu import install_track_menu

if TYPE_CHECKING:
    from ..application import JamjarApplication
    from ..window import JamjarWindow

log = logging.getLogger(__name__)


@Gtk.Template(resource_path="/land/rob/jamjar/queue-pane.ui")
class QueuePage(Adw.NavigationPage):
    __gtype_name__ = "JamjarQueuePage"

    sidebar_toggle = Gtk.Template.Child()
    queue_list     = Gtk.Template.Child()
    empty_state    = Gtk.Template.Child()
    queue_menu_button = Gtk.Template.Child()

    def __init__(self, app: JamjarApplication, window: JamjarWindow) -> None:
        super().__init__()
        self.app = app
        self.window = window
        # item_id -> heart Image so favorite-changed updates in O(1).
        # Multiple rows may share an id (same track twice in the queue);
        # we keep them in a list per id.
        self._row_hearts: dict[str, list[Gtk.Image]] = {}

        self.sidebar_toggle.connect("clicked", lambda *_: self.window.toggle_sidebar())

        if app.queue:
            app.queue.connect("queue-changed",   self._refresh)
            app.queue.connect("current-changed", self._refresh_current)
        app.connect("favorite-changed", self._on_favorite_changed_external)
        GLib.idle_add(self._refresh)

    def _refresh(self, *_args) -> bool:
        for child in list(self.queue_list):
            self.queue_list.remove(child)
        self._row_hearts.clear()

        if self.app.queue is None or not self.app.queue.tracks:
            self.empty_state.set_visible(True)
            self.queue_list.set_visible(False)
            return False

        self.empty_state.set_visible(False)
        self.queue_list.set_visible(True)
        current_index = self.app.queue.index
        for index, track in enumerate(self.app.queue.tracks):
            row = Adw.ActionRow(
                title=escape_markup(track.name),
                subtitle=escape_markup(track.primary_artist),
                activatable=True,
            )

            # Drag handle — a grab icon at the start of each row. The
            # whole row could act as a DragSource, but pressing on the
            # body of an ActionRow is a click-to-jump gesture; gating
            # the drag on the handle icon keeps both interactions
            # discoverable. The grab handle also signals to the user
            # that the row is reorderable, which a context-menu-only
            # affordance wouldn't.
            handle = Gtk.Image.new_from_icon_name("list-drag-handle-symbolic")
            handle.add_css_class("dim-label")
            handle.set_valign(Gtk.Align.CENTER)
            handle.set_tooltip_text("Drag to reorder")
            self._install_drag_source(handle, index)
            row.add_prefix(handle)

            num = Gtk.Label(label=str(index + 1), width_chars=3)
            num.add_css_class("dim-label")
            num.add_css_class("numeric")
            row.add_prefix(num)

            if index == current_index:
                speaker = Gtk.Image.new_from_icon_name("audio-volume-high-symbolic")
                row.add_suffix(speaker)

            heart = favorite_heart(bool(track.user_data.get("IsFavorite")))
            row.add_suffix(heart)
            self._row_hearts.setdefault(track.id, []).append(heart)

            duration = Gtk.Label(label=format_duration(track.duration_seconds))
            duration.add_css_class("dim-label")
            duration.add_css_class("numeric")
            row.add_suffix(duration)

            remove_btn = Gtk.Button.new_from_icon_name("user-trash-symbolic")
            remove_btn.add_css_class("flat")
            remove_btn.set_tooltip_text("Remove from queue")
            remove_btn.set_valign(Gtk.Align.CENTER)
            remove_btn.connect("clicked", lambda _b, i=index: self._remove(i))
            row.add_suffix(remove_btn)

            row.connect("activated", lambda _r, i=index: self._jump(i))
            install_track_menu(row, lambda t=track: t, self.app, self.window)
            self._install_drop_target(row, index)
            self.queue_list.append(row)
        return False

    # ------------------------------------------------------------------
    # Drag-to-reorder
    #
    # GTK4 doesn't have a built-in reorderable ListBox; we add a
    # DragSource on the grab handle and a DropTarget on each row,
    # exchanging the source index via an int-typed ContentProvider.
    # Queue.move() handles the play-head adjustment so the
    # currently-playing track stays "current" across a reorder.
    # ------------------------------------------------------------------

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
        # We accept ints (queue source index); dropping with src==target
        # is a no-op so the user doesn't have to release outside the row.
        target = Gtk.DropTarget.new(int, Gdk.DragAction.MOVE)

        def on_drop(_t, value, _x, _y):
            try:
                src_index = int(value)
            except (TypeError, ValueError):
                return False
            if src_index == target_index:
                return False
            if self.app.queue is None:
                return False
            self.app.queue.move(src_index, target_index)
            return True

        target.connect("drop", on_drop)
        row.add_controller(target)

    def _on_favorite_changed_external(self, _app, item_id: str, is_favorite: bool) -> None:
        for heart in self._row_hearts.get(item_id, ()):
            heart.set_visible(is_favorite)

    def _refresh_current(self, *_args) -> None:
        # Repaint to update the speaker indicator.
        self._refresh()

    def _jump(self, index: int) -> None:
        if self.app.queue is None:
            return
        # Player listens for the queue's `current-changed` signal and starts
        # playback automatically — no need to call player.play() here.
        self.app.queue.jump_to(index)

    def _remove(self, index: int) -> None:
        if self.app.queue is None:
            return
        self.app.queue.remove(index)
