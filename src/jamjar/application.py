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
from .offline import OfflineManager
from .player import Player
from .queue import PlayQueue
from .radio import RadioSession
from .scrobble import Scrobbler
from .searchprovider import SearchProvider
from .secrets import clear_token, lookup_token
from .sleep_timer import SleepTimer

log = logging.getLogger(__name__)

APP_ID = "land.rob.jamjar"

# Cap on how much queue is written to GSettings. A running radio station
# appends 60 tracks at a time, so an unbounded save would grow forever;
# the window is centred on the play head, which is the part that matters.
QUEUE_SAVE_LIMIT = 500
QUEUE_SAVE_DEBOUNCE_MS = 2000


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
        self.radio: RadioSession | None = None
        self.offline: OfflineManager | None = None
        self.sleep_timer = SleepTimer()
        self.search_provider = SearchProvider(self)
        self._holding = False
        self._settings_handlers: list[int] = []
        self._player_state_handler: int | None = None
        self._queue_save_source: int | None = None
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

    def do_dbus_register(self, connection, object_path: str) -> bool:  # type: ignore[override]
        # GNOME Shell talks to the search provider over the bus name
        # GApplication already owns, so this is the whole registration.
        self.search_provider.register(connection)
        return Adw.Application.do_dbus_register(self, connection, object_path)

    def do_dbus_unregister(self, connection, object_path: str) -> None:  # type: ignore[override]
        self.search_provider.unregister(connection)
        Adw.Application.do_dbus_unregister(self, connection, object_path)

    def do_activate(self) -> None:  # type: ignore[override]
        from .window import JamjarWindow
        win = self.props.active_window
        if win is None:
            win = JamjarWindow(application=self)
        win.present()

    def do_shutdown(self) -> None:  # type: ignore[override]
        self._save_playback_state()
        if self.player:
            self.player.close()
        if self.client:
            self.runner.submit(self.client.close())
        self.runner.stop()
        Adw.Application.do_shutdown(self)

    # ------- session wiring -------

    def attach_session(self, client: JellyfinClient) -> None:
        """Called after a successful login or token restore."""
        self.client = client
        client.on_unauthorized = self._on_client_unauthorized
        self.runner.submit(client.delete_expired_cache())
        self.library = Library(client, self.runner, on_error=self.show_toast)
        self.queue = PlayQueue(client)
        self.player = Player(self.queue)
        self.scrobbler = Scrobbler(client, self.player, self.queue, self.runner)

        # Restore persisted player state
        self.queue.shuffle = self.settings.get_boolean("shuffle")
        self.queue.repeat = int(self.settings.get_enum("repeat-mode"))
        self.player.configure(
            volume=self.settings.get_double("volume"),
            replaygain=self.settings.get_boolean("replaygain"),
            crossfade_seconds=self.settings.get_uint("crossfade-seconds"),
            crossfade_albums=self.settings.get_boolean("crossfade-albums"),
        )
        self._settings_handlers = [
            self.settings.connect("changed::replaygain", self._on_replaygain_changed),
            self.settings.connect("changed::crossfade-seconds", self._on_crossfade_changed),
            self.settings.connect("changed::crossfade-albums", self._on_crossfade_changed),
        ]

        win = self.props.active_window
        self.mpris = MprisService(
            self.player, self.queue, self.runner,
            on_quit=lambda: GLib.idle_add(self.quit),
            on_raise=lambda: GLib.idle_add(lambda: (win.present(), False)[1]) if win else None,
        )

        self.radio = RadioSession(self)
        self.offline = OfflineManager(client, self.runner)
        # The player checks this before every stream URL, so a downloaded
        # track plays from disk — and keeps playing with no network.
        self.player.offline = self.offline
        self._player_state_handler = self.player.connect("state-changed", self._on_player_state)
        self.queue.connect("queue-changed",   self._schedule_playback_save)
        self.queue.connect("current-changed", self._schedule_playback_save)
        self.sleep_timer.attach(self.player)
        self._update_session_actions()
        self._restore_playback_state()

    def detach_session(self) -> None:
        self.sleep_timer.detach()
        for handler in self._settings_handlers:
            self.settings.disconnect(handler)
        self._settings_handlers = []
        if self.player:
            if self._player_state_handler is not None:
                self.player.disconnect(self._player_state_handler)
                self._player_state_handler = None
            self.player.close()
        if self.scrobbler:
            self.scrobbler.stop()
        if self.mpris:
            self.mpris.close()
        if self.client:
            client = self.client

            async def shutdown():
                await client.clear_http_cache()
                await client.close()

            self.runner.submit(shutdown())
        self.client = None
        self.library = None
        self.queue = None
        self.player = None
        self.scrobbler = None
        self.mpris = None
        self.radio = None
        if self.offline is not None:
            self.offline.index.close()
            self.offline = None
        self._release_hold()
        self._update_session_actions()

    # ------- playback state persistence -------

    def _schedule_playback_save(self, *_args) -> None:
        if self._queue_save_source is not None:
            GLib.source_remove(self._queue_save_source)

        def flush() -> bool:
            self._queue_save_source = None
            self._save_playback_state()
            return False

        self._queue_save_source = GLib.timeout_add(QUEUE_SAVE_DEBOUNCE_MS, flush)

    def _save_playback_state(self) -> None:
        """Persist the queue so closing the app doesn't lose your place."""
        if self.queue is None:
            return
        tracks = self.queue.tracks
        index = self.queue.index
        if len(tracks) > QUEUE_SAVE_LIMIT:
            start = 0 if index < 0 else min(index, len(tracks) - QUEUE_SAVE_LIMIT)
            tracks = tracks[start:start + QUEUE_SAVE_LIMIT]
            index = -1 if index < 0 else index - start
        self.settings.set_strv("queue-track-ids", [t.id for t in tracks])
        self.settings.set_int("queue-index", index)
        self.settings.set_double(
            "queue-position", self.player.position if self.player else 0.0)

    def _restore_playback_state(self) -> None:
        ids = list(self.settings.get_strv("queue-track-ids"))
        if not ids or self.client is None:
            return
        index = self.settings.get_int("queue-index")
        position = self.settings.get_double("queue-position")
        current_id = ids[index] if 0 <= index < len(ids) else None
        client = self.client

        async def runme():
            return await client.tracks_by_ids(ids)

        def done(future):
            try:
                tracks = future.result()
            except Exception as e:
                # A queue we can't rebuild isn't worth a toast — the user
                # didn't ask for anything yet.
                log.info("queue restore skipped: %s", e)
                return
            GLib.idle_add(self._apply_restored_queue, tracks, current_id, position)

        self.runner.submit(runme()).add_done_callback(done)

    def _apply_restored_queue(self, tracks, current_id, position: float) -> bool:
        if not tracks or self.queue is None or self.player is None:
            return False
        if self.queue.tracks:
            # Something already started playing while the fetch was in
            # flight — leave it alone.
            return False
        index = next((i for i, t in enumerate(tracks) if t.id == current_id), 0)
        self.queue.restore(tracks, index)
        self.player.prepare(tracks[index], position)
        log.info("restored %d queued tracks at index %d", len(tracks), index)
        return False

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
            if win is None:
                return False
            # Fire the suite-standard win.toast(s) action so the window
            # owns the routing decision (Adw.Toast styling, dedupe, etc.).
            win.activate_action("toast", GLib.Variant("s", message))
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
        win.activate_action("toast",
            GLib.Variant("s", "Session expired — please sign in again"))
        if hasattr(win, "show_login"):
            win.show_login()
        return False

    # ------- actions -------

    # Actions whose enabled state depends on whether a Jellyfin session
    # is currently attached.
    SESSION_ACTIONS = ("logout", "switch-server", "search",
                       "toggle", "next", "previous", "sleep-timer",
                       "play-on", "volume-up", "volume-down")

    def _install_actions(self) -> None:
        actions: list[tuple[str, callable]] = [
            ("quit",          lambda *_: self.quit()),
            ("about",         lambda *_: self._show_about()),
            ("preferences",   lambda *_: self._show_preferences()),
            ("logout",        lambda *_: self._logout()),
            ("switch-server", lambda *_: self._logout()),
            ("search",        lambda *_: self._focus_search()),
            ("sleep-timer",   lambda *_: self._show_sleep_timer()),
            ("play-on",       lambda *_: self._show_remote_devices()),
        ]
        for name, callback in actions:
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", callback)
            self.add_action(action)

        # Player actions live on the application so accelerators work everywhere
        for name, fn in (
            ("toggle",      lambda *_: self.player and self.player.toggle()),
            ("next",        lambda *_: self.player and self.player.next()),
            ("previous",    lambda *_: self.player and self.player.previous()),
            ("volume-up",   lambda *_: self._adjust_volume(0.05)),
            ("volume-down", lambda *_: self._adjust_volume(-0.05)),
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
        self.set_accels_for_action("app.volume-up",   ["<Primary>Up"])
        self.set_accels_for_action("app.volume-down", ["<Primary>Down"])
        self.set_accels_for_action("win.show-help-overlay",
                                   ["<Primary>question"])
        # Bare space is bound at the window level, not as an app accel, so
        # text entries (search, etc.) can still receive space characters.

    # ------- side-effects from player state -------

    def _adjust_volume(self, delta: float) -> None:
        if self.player is None:
            return
        new = max(0.0, min(1.0, self.player.volume + delta))
        self.player.set_volume(new)
        self.settings.set_double("volume", new)

    def _on_replaygain_changed(self, settings, _key) -> None:
        if self.player is not None:
            self.player.set_replaygain(settings.get_boolean("replaygain"))

    def _on_crossfade_changed(self, settings, _key) -> None:
        if self.player is not None:
            self.player.set_crossfade(settings.get_uint("crossfade-seconds"))
            self.player.set_crossfade_albums(settings.get_boolean("crossfade-albums"))

    def _on_player_state(self, _player, state: str) -> None:
        if state in ("paused", "stopped"):
            self._schedule_playback_save()
        if state == "playing" and not self._holding:
            self.hold()
            self._holding = True
        elif state != "playing" and self._holding:
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

    def _show_remote_devices(self) -> None:
        from .views.remote import RemoteDevicesDialog
        win = self.props.active_window
        if win is None or self.client is None:
            return
        RemoteDevicesDialog(self).present(win)

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
