"""Downloaded tracks — the offline library.

Everything here is rendered from the local SQLite index, never the
server. That's the point: the page has to work on a train. Cover art
comes from the on-disk image cache for the same reason, so tiles fill in
for anything played before and stay blank rather than spinning for the
rest.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from gi.repository import Adw, GLib, Gtk

from ..offline import track_from_meta
from ._common import escape_markup, format_duration
from .track_menu import install_track_menu

if TYPE_CHECKING:
    from ..application import JamjarApplication
    from ..window import JamjarWindow

log = logging.getLogger(__name__)


def _format_size(num_bytes: int) -> str:
    mb = num_bytes / (1024 * 1024)
    if mb >= 1024:
        return f"{mb / 1024:.1f} GB"
    return f"{mb:.0f} MB"


class DownloadedPage(Adw.NavigationPage):
    __gtype_name__ = "JamjarDownloadedPage"

    def __init__(self, app: JamjarApplication, window: JamjarWindow) -> None:
        super().__init__(title="Downloaded", tag="downloaded")
        self.app = app
        self.window = window
        self._tracks: list = []

        self._list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE,
                                 margin_start=12, margin_end=12,
                                 margin_top=12, margin_bottom=12)
        self._list.add_css_class("boxed-list")

        self._empty = Adw.StatusPage(
            icon_name="folder-download-symbolic",
            title="Nothing downloaded",
            description="Download an album or a track and it plays without "
                        "the server — useful on a train.",
            vexpand=True,
        )

        self._stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        self._stack.add_named(self._empty, "empty")
        scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER,
                                      vexpand=True)
        clamp = Adw.Clamp(maximum_size=960)
        clamp.set_child(self._list)
        scroller.set_child(clamp)
        self._stack.add_named(scroller, "list")

        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        sidebar_toggle = Gtk.Button(icon_name="sidebar-show-symbolic",
                                    tooltip_text="Toggle Sidebar")
        sidebar_toggle.connect("clicked", lambda *_: window.toggle_sidebar())
        header.pack_start(sidebar_toggle)
        self._title = Adw.WindowTitle(title="Downloaded")
        header.set_title_widget(self._title)
        self._play_all = Gtk.Button(icon_name="media-playback-start-symbolic",
                                    tooltip_text="Play All")
        self._play_all.connect("clicked", lambda *_: self._play_all_clicked())
        header.pack_end(self._play_all)
        toolbar.add_top_bar(header)
        toolbar.set_content(self._stack)
        self.set_child(toolbar)

        if app.offline is not None:
            app.offline.connect("downloads-changed", lambda *_: self._refresh())
            app.offline.connect("progress", self._on_progress)
        GLib.idle_add(self._refresh)

    # ------- rendering -------

    def _on_progress(self, _offline, done: int, total: int) -> None:
        self._title.set_subtitle(f"Downloading {done} of {total}…" if done < total else "")

    def _refresh(self) -> bool:
        offline = self.app.offline
        for child in list(self._list):
            self._list.remove(child)
        if offline is None:
            self._stack.set_visible_child_name("empty")
            return False

        entries = offline.entries()
        self._tracks = [track_from_meta(e) for e in entries]
        self._play_all.set_sensitive(bool(self._tracks))
        total = offline.total_bytes()
        self._title.set_subtitle(
            f"{len(entries)} tracks · {_format_size(total)}" if entries else "")

        if not entries:
            self._stack.set_visible_child_name("empty")
            return False

        for index, (entry, track) in enumerate(zip(entries, self._tracks)):
            row = Adw.ActionRow(
                title=escape_markup(track.name or entry["item_id"]),
                subtitle=escape_markup(
                    " • ".join(p for p in (track.primary_artist, track.album) if p)),
                activatable=True,
            )
            size = Gtk.Label(label=_format_size(entry["size"]))
            size.add_css_class("dim-label")
            size.add_css_class("numeric")
            row.add_suffix(size)
            if track.duration_seconds:
                duration = Gtk.Label(label=format_duration(track.duration_seconds))
                duration.add_css_class("dim-label")
                duration.add_css_class("numeric")
                row.add_suffix(duration)
            remove = Gtk.Button(icon_name="user-trash-symbolic",
                                tooltip_text="Remove Download",
                                valign=Gtk.Align.CENTER)
            remove.add_css_class("flat")
            remove.connect("clicked",
                           lambda _b, item_id=entry["item_id"]: self._remove(item_id))
            row.add_suffix(remove)
            row.connect("activated", lambda _r, i=index: self._play_from(i))
            install_track_menu(row, lambda t=track: t, self.app, self.window)
            self._list.append(row)

        self._stack.set_visible_child_name("list")
        return False

    # ------- actions -------

    def _remove(self, item_id: str) -> None:
        if self.app.offline is not None:
            self.app.offline.remove(item_id)

    def _play_from(self, index: int) -> None:
        if not self._tracks or self.app.queue is None:
            return
        self.app.queue.replace(self._tracks, start_index=index)

    def _play_all_clicked(self) -> None:
        self._play_from(0)
