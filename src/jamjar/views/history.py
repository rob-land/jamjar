"""Recently played tracks — full list beyond the home shelf."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from gi.repository import Adw, GLib, Gtk

from ._common import escape_markup, format_duration
from .track_menu import install_track_menu
if TYPE_CHECKING:
    from ..application import JamjarApplication
    from ..window import JamjarWindow

log = logging.getLogger(__name__)


@Gtk.Template(resource_path="/land/rob/jamjar/history-page.ui")
class HistoryPage(Adw.NavigationPage):
    __gtype_name__ = "JamjarHistoryPage"

    sidebar_toggle = Gtk.Template.Child()
    history_list   = Gtk.Template.Child()
    empty_state    = Gtk.Template.Child()

    def __init__(self, app: JamjarApplication, window: JamjarWindow) -> None:
        super().__init__()
        self.app = app
        self.window = window
        self.sidebar_toggle.connect("clicked", lambda *_: window.toggle_sidebar())
        self._tracks: list = []
        GLib.idle_add(self._load)

    def _load(self) -> bool:
        if self.app.library is None:
            return False

        async def runme():
            return await self.app.client.recently_played_tracks(limit=200)

        def done(future):
            try:
                tracks = future.result()
            except Exception as e:
                log.warning("history load failed: %s", e)
                tracks = []
                if self.app.show_toast:
                    self.app.show_toast("Couldn't load play history.")
            GLib.idle_add(lambda: (self._apply(tracks), False)[1])

        self.app.runner.submit(runme()).add_done_callback(done)
        return False

    def _apply(self, tracks) -> bool:
        for child in list(self.history_list):
            self.history_list.remove(child)
        self._tracks = tracks
        if not tracks:
            self.empty_state.set_visible(True)
            self.history_list.set_visible(False)
            return False
        self.empty_state.set_visible(False)
        self.history_list.set_visible(True)
        for index, track in enumerate(tracks):
            row = Adw.ActionRow(
                title=escape_markup(track.name),
                subtitle=escape_markup(track.primary_artist),
                activatable=True,
            )
            num = Gtk.Label(label=str(index + 1), width_chars=3)
            num.add_css_class("dim-label")
            num.add_css_class("numeric")
            row.add_prefix(num)
            duration = Gtk.Label(label=format_duration(track.duration_seconds))
            duration.add_css_class("dim-label")
            duration.add_css_class("numeric")
            row.add_suffix(duration)
            row.connect("activated", lambda _r, t=track: self._play(t))
            install_track_menu(row, lambda t=track: t, self.app, self.window)
            self.history_list.append(row)
        return False

    def _play(self, track) -> None:
        if self.app.queue is None or self.app.player is None:
            return
        self.app.queue.replace([track], start_index=0)
        self.app.player.play(track)
