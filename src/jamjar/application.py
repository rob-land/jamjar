"""Adw.Application subclass — creates the session, owns global services."""

from __future__ import annotations

import logging
import time
from gettext import gettext as _

from gi.repository import Adw, Gio, GLib, GObject, Gtk

from . import __version__, imagecache
from .auth import new_device_id
from .client import AsyncRunner, JellyfinClient
from .library import Library
from .mpris import MprisService
from .player import Player
from .queue import PlayQueue
from .scrobble import Scrobbler
from .secrets import clear_token, lookup_token
from .sleep_timer import SleepTimer

log = logging.getLogger(__name__)

APP_ID = "land.rob.jamjar"


class JamjarApplication(Adw.Application):
    __gtype_name__ = "JamjarApplication"
    __gsignals__ = {
        # Emitted after a successful favorite toggle on any item (track,
        # album, artist). Surfaces displaying the same item subscribe and
        # update their visual without each having to round-trip through
        # the server. Args: (item_id, is_favorite).
        "favorite-changed": (GObject.SignalFlags.RUN_FIRST, None, (str, bool)),
    }

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

        self.client: JellyfinClient | None = None
        self.library: Library | None = None
        self.queue: PlayQueue | None = None
        self.player: Player | None = None
        self.scrobbler: Scrobbler | None = None
        self.mpris: MprisService | None = None
        self.sleep_timer = SleepTimer()
        self._holding = False
        # Per-message dedup so a wave of related failures (e.g. losing
        # network mid-prefetch) doesn't stack four toasts on the user.
        self._last_toast_message: str = ""
        self._last_toast_at: float = 0.0

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
        self.library = Library(client, self.runner, on_error=self.show_toast)
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

    # ------- favorite cross-surface sync -------

    def emit_favorite_changed(self, item_id: str, is_favorite: bool) -> None:
        """Broadcast a favorite-state change to all subscribed surfaces.

        Safe to call from any thread; idle_adds onto the GTK loop before
        emitting. Callers should invoke this only after the server has
        confirmed the change so subscribers don't show false-positive
        states on REST failures.
        """
        GLib.idle_add(self._do_emit_favorite, item_id, is_favorite)

    def _do_emit_favorite(self, item_id: str, is_favorite: bool) -> bool:
        self.emit("favorite-changed", item_id, is_favorite)
        return False

    # ------- toast helper -------

    TOAST_DEDUP_SECONDS = 5.0

    def show_toast(self, message: str, *, timeout: int = 4) -> None:
        """Display a transient toast on the active window.

        Safe to call from any thread; marshals to the GTK loop. Suppresses
        the same message within `TOAST_DEDUP_SECONDS` of its previous
        appearance so failure waves (e.g. several sections failing at
        once when the network drops) don't stack toasts.
        """
        def emit() -> bool:
            now = time.monotonic()
            if (message == self._last_toast_message
                    and now - self._last_toast_at < self.TOAST_DEDUP_SECONDS):
                return False
            self._last_toast_message = message
            self._last_toast_at = now
            win = self.props.active_window
            if win is None or not hasattr(win, "toast_overlay"):
                return False
            toast = Adw.Toast.new(message)
            toast.set_timeout(timeout)
            win.toast_overlay.add_toast(toast)
            return False

        GLib.idle_add(emit)

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
        self.set_accels_for_action("win.show-help-overlay",
                                   ["<Primary>question"])
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
            provider.load_from_resource("/land/rob/jamjar/style.css")
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
            developer_name="Rob Daniel",
            developers=["Rob Daniel"],
            version=__version__,
            website="https://codeberg.org/robland/jamjar",
            issue_url="https://codeberg.org/robland/jamjar/issues",
            license_type=Gtk.License.GPL_3_0,
            copyright="© 2026 Rob Daniel",
            comments="A Jellyfin music client for GNOME and Phosh.",
            # The literal "translator-credits" string is the canonical
            # i18n marker. Translators replace it with their own names;
            # untranslated, Adw.AboutDialog hides the credits panel.
            translator_credits=_("translator-credits"),
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

    def try_restore_session(self, on_done) -> None:
        """Attempt to restore a saved session in the background.

        Calls `on_done(True)` after `attach_session` has run on the
        GTK thread, or `on_done(False)` if no saved credentials or the
        restore failed. Always invokes `on_done` on the main thread so
        callers can present the login dialog from the failure branch.
        """
        sid = self.settings.get_string("last-server-id")
        uid = self.settings.get_string("last-user-id")
        addr = self.settings.get_string("last-server-address")
        if not (sid and uid and addr):
            on_done(False)
            return
        token = lookup_token(sid, uid)
        if not token:
            on_done(False)
            return
        device_id = self.settings.get_string("device-id")

        async def build():
            client = JellyfinClient(addr, uid, token, device_id)
            await client.__aenter__()
            return client

        def _done(future):
            try:
                client = future.result()
            except Exception as e:
                log.warning("session restore failed: %s", e)
                GLib.idle_add(on_done, False)
                return

            def _attach_and_signal():
                self.attach_session(client)
                on_done(True)
                return False
            GLib.idle_add(_attach_and_signal)

        self.runner.submit(build()).add_done_callback(_done)
