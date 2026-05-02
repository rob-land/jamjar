"""Adw.Application subclass — creates the session, owns global services."""

from __future__ import annotations

import logging
import uuid
from typing import Optional

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk

from . import __version__, imagecache
from .auth import new_device_id
from .client import AsyncRunner, JellyfinClient
from .library import Library
from .mpris import MprisService
from .player import Player
from .queue import PlayQueue, RepeatMode
from .scrobble import Scrobbler
from .secrets import lookup_token, clear_token
from .sleep_timer import SleepTimer

log = logging.getLogger(__name__)

APP_ID = "land.rob.Jamjar"


class JamjarApplication(Adw.Application):
    __gtype_name__ = "JamjarApplication"

    def __init__(self) -> None:
        super().__init__(
            application_id=APP_ID,
            flags=Gio.ApplicationFlags.DEFAULT_FLAGS,
        )

        self.runner = AsyncRunner()
        self.settings = Gio.Settings.new(APP_ID)

        # Ensure we have a stable device id
        if not self.settings.get_string("device-id"):
            self.settings.set_string("device-id", new_device_id())

        self.client: Optional[JellyfinClient] = None
        self.library: Optional[Library] = None
        self.queue: Optional[PlayQueue] = None
        self.player: Optional[Player] = None
        self.scrobbler: Optional[Scrobbler] = None
        self.mpris: Optional[MprisService] = None
        self.sleep_timer = SleepTimer()
        self._holding = False

    # ------- lifecycle -------

    def do_startup(self) -> None:  # type: ignore[override]
        Adw.Application.do_startup(self)
        self._install_actions()
        self._install_accelerators()
        self._apply_color_scheme()
        self._load_css()
        imagecache.schedule_prune()

    def do_activate(self) -> None:  # type: ignore[override]
        from .window import JamjarWindow
        win = self.props.active_window
        if win is None:
            win = JamjarWindow(application=self)
        win.present()

    def do_shutdown(self) -> None:  # type: ignore[override]
        if self.player:
            self.player.stop()
        if self.client:
            self.runner.submit(self.client.close())
        self.runner.stop()
        Adw.Application.do_shutdown(self)

    # ------- session wiring -------

    def attach_session(self, client: JellyfinClient) -> None:
        """Called after a successful login or token restore."""
        self.client = client
        client.on_unauthorized = self._on_client_unauthorized
        self.library = Library(client, self.runner)
        self.queue = PlayQueue(client)
        self.player = Player(self.queue)
        self.scrobbler = Scrobbler(client, self.player, self.queue, self.runner)

        # Restore persisted player state
        self.queue.shuffle = self.settings.get_boolean("shuffle")
        self.queue.repeat = int(self.settings.get_enum("repeat-mode"))
        self.player.configure(volume=self.settings.get_double("volume"))

        win = self.props.active_window
        self.mpris = MprisService(
            self.player, self.queue, self.runner,
            on_quit=lambda: GLib.idle_add(self.quit),
            on_raise=lambda: GLib.idle_add(lambda: (win.present(), False)[1]) if win else None,
        )

        self.player.connect("state-changed", self._on_player_state)
        self.sleep_timer.attach(self.player)
        self._update_session_actions()

    def detach_session(self) -> None:
        self.sleep_timer.detach()
        if self.player:
            self.player.stop()
        if self.client:
            self.runner.submit(self.client.close())
        self.client = None
        self.library = None
        self.queue = None
        self.player = None
        self.scrobbler = None
        self.mpris = None
        self._release_hold()
        self._update_session_actions()

    # ------- session-lost handling -------

    def _on_client_unauthorized(self) -> None:
        # Called from the asyncio thread, possibly many times in quick
        # succession when several requests 401 in parallel. Marshal back to
        # GTK with idle_add — _do_unauthorized dedupes there.
        GLib.idle_add(self._do_unauthorized)

    def _do_unauthorized(self) -> bool:
        # Dedupe: once the client is gone, subsequent 401s are no-ops.
        if self.client is None:
            return False
        log.warning("session lost (401) — clearing token and re-prompting")
        sid = self.settings.get_string("last-server-id")
        uid = self.settings.get_string("last-user-id")
        if sid and uid:
            clear_token(sid, uid)
        # Keep last-server-address so the login dialog re-targets the same
        # server without making the user re-discover it.
        self.detach_session()
        win = self.props.active_window
        if win is None:
            return False
        if hasattr(win, "toast_overlay"):
            toast = Adw.Toast.new("Session expired — please sign in again")
            toast.set_timeout(4)
            win.toast_overlay.add_toast(toast)
        if hasattr(win, "show_login"):
            win.show_login()
        return False

    # ------- actions -------

    # Actions whose enabled state depends on whether a Jellyfin session
    # is currently attached.
    SESSION_ACTIONS = ("logout", "switch-server", "search",
                       "toggle", "next", "previous", "sleep-timer")

    def _install_actions(self) -> None:
        actions: list[tuple[str, callable]] = [
            ("quit",          lambda *_: self.quit()),
            ("about",         lambda *_: self._show_about()),
            ("preferences",   lambda *_: self._show_preferences()),
            ("logout",        lambda *_: self._logout()),
            ("switch-server", lambda *_: self._logout()),
            ("search",        lambda *_: self._focus_search()),
            ("sleep-timer",   lambda *_: self._show_sleep_timer()),
        ]
        for name, callback in actions:
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", callback)
            self.add_action(action)

        # Player actions live on the application so accelerators work everywhere
        for name, fn in (
            ("toggle",   lambda *_: self.player and self.player.toggle()),
            ("next",     lambda *_: self.player and self.player.next()),
            ("previous", lambda *_: self.player and self.player.previous()),
        ):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", fn)
            self.add_action(action)

        self._update_session_actions()

    def _update_session_actions(self) -> None:
        has_session = self.client is not None
        for name in self.SESSION_ACTIONS:
            action = self.lookup_action(name)
            if action is not None:
                action.set_enabled(has_session)

    def _install_accelerators(self) -> None:
        self.set_accels_for_action("app.quit",        ["<Primary>q"])
        self.set_accels_for_action("app.search",      ["<Primary>f"])
        self.set_accels_for_action("app.next",        ["<Primary>Right"])
        self.set_accels_for_action("app.previous",    ["<Primary>Left"])
        self.set_accels_for_action("app.preferences", ["<Primary>comma"])
        # Bare space is bound at the window level, not as an app accel, so
        # text entries (search, etc.) can still receive space characters.

    # ------- side-effects from player state -------

    def _on_player_state(self, _player, state: str) -> None:
        if state == "playing" and not self._holding:
            self.hold()
            self._holding = True
        elif state == "stopped" and self._holding:
            self._release_hold()

    def _release_hold(self) -> None:
        if self._holding:
            self.release()
            self._holding = False

    # ------- prefs / about / search -------

    def _apply_color_scheme(self) -> None:
        scheme = self.settings.get_enum("color-scheme")
        manager = Adw.StyleManager.get_default()
        if scheme == 1:
            manager.set_color_scheme(Adw.ColorScheme.FORCE_LIGHT)
        elif scheme == 2:
            manager.set_color_scheme(Adw.ColorScheme.FORCE_DARK)
        else:
            manager.set_color_scheme(Adw.ColorScheme.DEFAULT)

    def _load_css(self) -> None:
        provider = Gtk.CssProvider()
        try:
            provider.load_from_resource("/land/rob/Jamjar/style.css")
        except GLib.Error as e:
            log.debug("CSS not loaded: %s", e.message)
            return
        display = self.props.active_window.get_display() if self.props.active_window else None
        if display is None:
            from gi.repository import Gdk
            display = Gdk.Display.get_default()
        if display:
            Gtk.StyleContext.add_provider_for_display(
                display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
            )

    def _show_about(self) -> None:
        about = Adw.AboutDialog(
            application_name="Jamjar",
            application_icon=APP_ID,
            developer_name="Rob",
            version=__version__,
            website="https://github.com/rob/jamjar",
            issue_url="https://github.com/rob/jamjar/issues",
            license_type=Gtk.License.GPL_3_0,
            comments="A Jellyfin music client for GNOME and Phosh.",
        )
        about.present(self.props.active_window)

    def _show_preferences(self) -> None:
        from .views.prefs import PreferencesDialog
        dialog = PreferencesDialog(self.settings)
        dialog.present(self.props.active_window)

    def _show_sleep_timer(self) -> None:
        from .views.sleep_timer import SleepTimerDialog
        dialog = SleepTimerDialog(self)
        dialog.present(self.props.active_window)

    def _focus_search(self) -> None:
        win = self.props.active_window
        if win and hasattr(win, "focus_search"):
            win.focus_search()

    def _logout(self) -> None:
        sid = self.settings.get_string("last-server-id")
        uid = self.settings.get_string("last-user-id")
        if sid and uid:
            clear_token(sid, uid)
        self.settings.set_string("last-server-id", "")
        self.settings.set_string("last-user-id", "")
        self.settings.set_string("last-server-address", "")
        self.detach_session()
        win = self.props.active_window
        if win and hasattr(win, "show_login"):
            win.show_login()

    # ------- token restore -------

    def try_restore_session(self) -> bool:
        sid = self.settings.get_string("last-server-id")
        uid = self.settings.get_string("last-user-id")
        addr = self.settings.get_string("last-server-address")
        if not (sid and uid and addr):
            return False
        token = lookup_token(sid, uid)
        if not token:
            return False
        device_id = self.settings.get_string("device-id")

        async def build():
            client = JellyfinClient(addr, uid, token, device_id)
            await client.__aenter__()
            return client

        try:
            client = self.runner.submit(build()).result(timeout=4.0)
        except Exception as e:
            log.warning("session restore failed: %s", e)
            return False
        self.attach_session(client)
        return True
