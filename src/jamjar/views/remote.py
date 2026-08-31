"""Hand playback to another Jellyfin client — "Play on…".

Jellyfin's /Sessions API lets one client drive another, which is the
self-hosted answer to casting: no UPnP, no discovery protocol, just the
server telling the TV (or the desktop, or the phone) what to play. This
dialog lists the sessions that accept remote control and pushes the
current queue to whichever one you pick.

Scope is deliberately one-way: the queue is handed over and local
playback pauses. Driving the far end afterwards (transport controls,
progress) is a bigger feature — this is "continue in the other room".
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from gi.repository import Adw, GLib, Gtk

from ._common import escape_markup

if TYPE_CHECKING:
    from ..application import JamjarApplication

log = logging.getLogger(__name__)

# Jellyfin positions are in ticks: 1 tick = 100 ns.
TICKS_PER_SECOND = 10_000_000
# Enough to hand over a long listening session without an unwieldy URL.
MAX_PUSHED_TRACKS = 200


class RemoteDevicesDialog(Adw.Dialog):
    __gtype_name__ = "JamjarRemoteDevicesDialog"

    def __init__(self, app: JamjarApplication) -> None:
        super().__init__()
        self.app = app
        self.set_title("Play On")
        self.set_content_width(400)
        self.set_content_height(420)

        self._list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE,
                                 margin_start=12, margin_end=12,
                                 margin_top=12, margin_bottom=12)
        self._list.add_css_class("boxed-list")

        self._status = Adw.StatusPage(
            icon_name="network-wireless-symbolic",
            title="Looking for devices…",
            description="Other Jellyfin clients signed in as you appear here.",
        )

        self._stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        self._stack.add_named(self._status, "status")
        scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER,
                                      vexpand=True)
        scroller.set_child(self._list)
        self._stack.add_named(scroller, "devices")

        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        refresh = Gtk.Button(icon_name="view-refresh-symbolic",
                             tooltip_text="Refresh")
        refresh.add_css_class("flat")
        refresh.connect("clicked", lambda *_: self._load())
        header.pack_end(refresh)
        toolbar.add_top_bar(header)
        toolbar.set_content(self._stack)
        self.set_child(toolbar)

        self._load()

    # ------- loading -------

    def _load(self) -> None:
        if self.app.client is None:
            return
        client = self.app.client

        async def runme():
            return await client.remote_sessions()

        def done(future):
            try:
                sessions = future.result()
            except Exception as e:
                log.warning("session list failed: %s", e)
                GLib.idle_add(self._show_status, "Couldn't reach the server",
                              "Check the connection and try again.")
                return
            GLib.idle_add(self._render, sessions)

        self.app.runner.submit(runme()).add_done_callback(done)

    def _show_status(self, title: str, description: str) -> bool:
        self._status.set_title(title)
        self._status.set_description(description)
        self._stack.set_visible_child_name("status")
        return False

    def _render(self, sessions: list[dict]) -> bool:
        for child in list(self._list):
            self._list.remove(child)
        if not sessions:
            return self._show_status(
                "No other devices",
                "Open Jellyfin somewhere else — a browser, a TV app — and it "
                "will show up here.")

        for session in sessions:
            name = session.get("DeviceName") or session.get("Client") or "Device"
            client_name = session.get("Client") or ""
            playing = (session.get("NowPlayingItem") or {}).get("Name")
            subtitle = f"{client_name} · {playing}" if playing else client_name
            row = Adw.ActionRow(title=escape_markup(name),
                                subtitle=escape_markup(subtitle),
                                activatable=True)
            row.add_prefix(Gtk.Image.new_from_icon_name(_icon_for(session)))
            row.add_suffix(Gtk.Image.new_from_icon_name("go-next-symbolic"))
            row.connect("activated", lambda _r, s=session: self._push_to(s))
            self._list.append(row)
        self._stack.set_visible_child_name("devices")
        return False

    # ------- handover -------

    def _push_to(self, session: dict) -> None:
        queue, player = self.app.queue, self.app.player
        if self.app.client is None or queue is None or not len(queue):
            return
        start = max(0, queue.index)
        tracks = queue.tracks[start:start + MAX_PUSHED_TRACKS]
        item_ids = [t.id for t in tracks]
        position = int((player.position if player else 0.0) * TICKS_PER_SECOND)
        session_id = session.get("Id")
        name = session.get("DeviceName") or session.get("Client") or "device"
        client = self.app.client

        async def runme():
            await client.play_on_session(session_id, item_ids,
                                         position_ticks=position)

        def done(future):
            try:
                future.result()
            except Exception as e:
                log.warning("handover to %s failed: %s", session_id, e)
                self.app.show_toast(f"Couldn't start playback on {name}.")
                return
            # Two things playing the same track in two rooms is nobody's
            # idea of handover.
            GLib.idle_add(self._finish_handover, name)

        self.app.runner.submit(runme()).add_done_callback(done)

    def _finish_handover(self, name: str) -> bool:
        if self.app.player is not None and self.app.player.is_playing:
            self.app.player.pause()
        self.app.show_toast(f"Playing on {name}")
        self.close()
        return False


def _icon_for(session: dict) -> str:
    """Best guess at a device icon from the client name Jellyfin reports."""
    client = (session.get("Client") or "").lower()
    if any(word in client for word in ("android tv", "tv", "kodi", "roku")):
        return "tv-symbolic"
    if any(word in client for word in ("android", "ios", "iphone", "mobile")):
        return "phone-symbolic"
    if "web" in client or "browser" in client:
        return "web-browser-symbolic"
    return "computer-symbolic"
