"""Main application window — sidebar + content + now-playing bar."""

from __future__ import annotations

import logging
import time
from gettext import gettext as _
from typing import TYPE_CHECKING

from gi.repository import Adw, Gdk, Gio, GLib, Gtk

if TYPE_CHECKING:
    from .application import JamjarApplication

from .views.album import AlbumPage
from .views.artist import ArtistPage
from .views.history import HistoryPage
from .views.home import HomePage
from .views.library import LibraryPage
from .views.login import LoginDialog
from .views.now_playing import NowPlayingPage
from .views.playlist import PlaylistPage
from .views.queue import QueuePage
from .views.radio import RadioPage
from .views.search import SearchPage

log = logging.getLogger(__name__)

SIDEBAR_PAGES: list[tuple[str, str, str]] = [
    ("home",        "user-home-symbolic",                    "Home"),
    ("history",     "clock-symbolic",                        "History"),
    ("library",     "folder-music-symbolic",                 "Library"),
    ("radio",       "media-playlist-shuffle-symbolic",       "Radio"),
    ("now-playing", "media-playback-start-symbolic",         "Now Playing"),
    ("queue",       "view-list-symbolic",                    "Queue"),
    ("downloaded",  "folder-download-symbolic",              "Downloaded"),
]
# Search is reachable from the magnifying-glass button on each top-level
# page's header (matches the GNOME convention) and via Ctrl+F. It's still
# a NavigationView destination — `_build_page` builds the SearchPage —
# but no longer occupies a sidebar slot.


@Gtk.Template(resource_path="/land/rob/jamjar/window.ui")
class JamjarWindow(Adw.ApplicationWindow):
    __gtype_name__ = "JamjarWindow"

    split_view       = Gtk.Template.Child("split_view")
    sidebar_list     = Gtk.Template.Child("sidebar_list")
    nav_view         = Gtk.Template.Child("nav_view")
    now_playing_bar  = Gtk.Template.Child("now_playing_bar")
    content_page     = Gtk.Template.Child("content_page")
    toast_overlay    = Gtk.Template.Child("toast_overlay")

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.app: JamjarApplication = self.get_application()  # type: ignore[assignment]
        self._pages: dict[str, Adw.NavigationPage] = {}
        self._current_top_level: str = "home"
        self._shortcuts_dialog: Adw.ShortcutsDialog | None = None
        self._install_shortcuts_dialog()
        self._build_sidebar()

        self.connect("close-request", self._on_close_request)

        # Bare-space play/pause, suppressed when a text-editable widget has
        # focus so the search bar (etc.) still receives space characters.
        space_ctrl = Gtk.EventControllerKey()
        space_ctrl.connect("key-pressed", self._on_window_key_pressed)
        self.add_controller(space_ctrl)

        # Add a window-scoped action used by the Queue page menu.
        clear_action = Gio.SimpleAction.new("clear-queue", None)
        clear_action.connect("activate", lambda *_: self.app.queue and self.app.queue.clear())
        self.add_action(clear_action)

        save_queue_action = Gio.SimpleAction.new("save-queue", None)
        save_queue_action.connect("activate", lambda *_: self._save_queue_as_playlist())
        self.add_action(save_queue_action)

        # Suite-standard window action: any child widget can fire a
        # toast via widget.activate_action("win.toast", GLib.Variant("s", msg)).
        toast_action = Gio.SimpleAction.new("toast", GLib.VariantType.new("s"))
        toast_action.connect("activate",
            lambda _a, p: self.toast_overlay.add_toast(Adw.Toast.new(p.get_string())))
        self.add_action(toast_action)

        # Restore window size
        s = self.app.settings
        self.set_default_size(s.get_int("window-width"), s.get_int("window-height"))
        if s.get_boolean("window-maximized"):
            self.maximize()
        self.connect("notify::default-width",   self._save_geometry)
        self.connect("notify::default-height",  self._save_geometry)
        self.connect("notify::maximized",       self._save_geometry)

        GLib.idle_add(self._post_present)

    # ------- bootstrap -------

    def _install_shortcuts_dialog(self) -> None:
        builder = Gtk.Builder.new_from_resource("/land/rob/jamjar/help-overlay.ui")
        self._shortcuts_dialog = builder.get_object("help_overlay")

        action = Gio.SimpleAction.new("show-help-overlay", None)
        action.connect("activate", self._show_shortcuts_dialog)
        self.add_action(action)

    def _show_shortcuts_dialog(self, *_args) -> None:
        if self._shortcuts_dialog is not None:
            self._shortcuts_dialog.present(self)

    def _post_present(self) -> bool:
        def _after(restored: bool):
            if restored:
                self._on_session_attached()
            else:
                self.show_login()
        self.app.try_restore_session(_after)
        return False

    def _on_session_attached(self) -> None:
        # Drop any pages built against a previous session — their list models
        # and bindings reference stores from the now-detached Library.
        self._pages.clear()
        self.now_playing_bar.attach(self.app.player, self.app.queue, self.app.client)
        self.app.player.connect("track-changed", lambda *_: self._update_bar_visibility())
        self.nav_view.connect("notify::visible-page",
                              lambda *_: self._update_bar_visibility())
        self._show_page("home")
        self._update_bar_visibility()

    def show_login(self) -> None:
        dialog = LoginDialog(self.app)
        dialog.connect("authenticated", self._on_login_done)
        dialog.present(self)

    def _on_login_done(self, _dialog) -> None:
        self._on_session_attached()
        toast = Adw.Toast.new("Connected")
        toast.set_timeout(2)
        self.toast_overlay.add_toast(toast)

    # ------- sidebar -------

    def _build_sidebar(self) -> None:
        for name, icon, title in SIDEBAR_PAGES:
            row = Gtk.ListBoxRow()
            box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10,
                          margin_top=6, margin_bottom=6,
                          margin_start=10, margin_end=10)
            box.append(Gtk.Image.new_from_icon_name(icon))
            box.append(Gtk.Label(label=title, xalign=0, hexpand=True))
            row.set_child(box)
            row.target_name = name  # type: ignore[attr-defined]
            self.sidebar_list.append(row)
        self.sidebar_list.connect("row-activated", self._on_sidebar_row)

    def _on_sidebar_row(self, _box, row) -> None:
        if not hasattr(row, "target_name"):
            return
        self._show_page(row.target_name)
        if self.split_view.get_collapsed():
            self.split_view.set_show_sidebar(False)

    def toggle_sidebar(self) -> None:
        self.split_view.set_show_sidebar(not self.split_view.get_show_sidebar())

    # ------- pages -------

    def _show_page(self, name: str) -> None:
        if self.app.client is None:
            return
        page = self._pages.get(name)
        if page is None:
            page = self._build_page(name)
            if page is None:
                return
            self._pages[name] = page
        self._current_top_level = name
        # Reset stack to root with the requested page.
        self.nav_view.replace([page])
        self._sync_sidebar_selection(name)

    def _sync_sidebar_selection(self, name: str) -> None:
        # Highlight the sidebar row matching the active top-level page.
        for row in self.sidebar_list:
            if getattr(row, "target_name", None) == name:
                self.sidebar_list.select_row(row)
                return

    def _build_page(self, name: str) -> Adw.NavigationPage | None:
        if name == "home":
            return HomePage(self.app, self)
        if name == "history":
            return HistoryPage(self.app, self)
        if name == "library":
            return LibraryPage(self.app, self)
        if name == "radio":
            return RadioPage(self.app, self)
        if name == "search":
            return SearchPage(self.app, self)
        if name == "now-playing":
            return NowPlayingPage(self.app, self)
        if name == "queue":
            return QueuePage(self.app, self)
        if name == "downloaded":
            page = Adw.NavigationPage(title="Downloaded")
            tv = Adw.ToolbarView()
            header = Adw.HeaderBar()
            tv.add_top_bar(header)
            tv.set_content(Adw.StatusPage(
                title="Downloads coming in v0.3",
                description="Make albums and playlists available offline.",
                icon_name="folder-download-symbolic",
            ))
            page.set_child(tv)
            return page
        return None

    def show_now_playing(self) -> None:
        """Navigate to the Now Playing top-level page (called from the bar)."""
        self._show_page("now-playing")

    def push_page(self, page: Adw.NavigationPage) -> None:
        self.nav_view.push(page)

    def _update_bar_visibility(self) -> None:
        # Bar is visible only when something's playing AND we're not already
        # on the Now Playing page.
        page = self.nav_view.get_visible_page()
        on_now_playing = page is not None and page.get_tag() == "now-playing"
        has_track = bool(self.app.player and self.app.queue and self.app.queue.current)
        self.now_playing_bar.set_visible(has_track and not on_now_playing)

    def open_album(self, album) -> None:
        page = AlbumPage(self.app, self, album)
        self.push_page(page)

    def open_artist(self, artist) -> None:
        page = ArtistPage(self.app, self, artist)
        self.push_page(page)

    def open_playlist(self, playlist) -> None:
        page = PlaylistPage(self.app, self, playlist)
        self.push_page(page)

    # ------- search -------

    def focus_search(self) -> None:
        self._show_page("search")
        page = self._pages.get("search")
        if isinstance(page, SearchPage):
            page.focus_entry()

    def open_search(self, query: str = "") -> None:
        """Show the search page, optionally prefilled — the GNOME Shell
        search provider's "show all results" lands here."""
        self.focus_search()
        page = self._pages.get("search")
        if isinstance(page, SearchPage) and query:
            page.set_query(query)

    def open_search_hit(self, hit) -> None:
        """Open one result chosen in the Shell overview."""
        self._show_page("search")
        page = self._pages.get("search")
        if isinstance(page, SearchPage):
            page.activate_hit(hit)

    # ------- close behaviour -------

    def _on_window_key_pressed(self, _ctrl, keyval: int, _keycode: int,
                                state: Gdk.ModifierType) -> bool:
        if keyval != Gdk.KEY_space:
            return False
        if state & (Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.ALT_MASK
                    | Gdk.ModifierType.SUPER_MASK):
            return False
        focus = self.get_focus()
        if isinstance(focus, Gtk.Editable):
            return False
        if self.app.player:
            self.app.player.toggle()
        return True

    def _save_queue_as_playlist(self) -> None:
        """Turn whatever is queued into a real Jellyfin playlist.

        Most useful after a radio station has been running: the station
        found the tracks, this keeps them. The default name follows the
        station when one is playing.
        """
        app = self.app
        if app.client is None or app.queue is None or not len(app.queue):
            app.show_toast(_("Nothing in the queue to save."))
            return
        station = app.radio.station if app.radio else None
        default_name = station.title if station else time.strftime("Queue %-d %b %Y")

        entry = Gtk.Entry(text=default_name)
        entry.set_placeholder_text(_("Playlist name"))
        dialog = Adw.AlertDialog(
            heading=_("Save Queue as Playlist"),
            body=_("%d tracks will be saved.") % len(app.queue),
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
            if not name:
                app.show_toast(_("Enter a playlist name."))
                return
            self._create_queue_playlist(name)

        dialog.connect("response", on_response)
        dialog.present(self)

    def _create_queue_playlist(self, name: str) -> None:
        app = self.app
        item_ids = [t.id for t in app.queue.tracks]
        client = app.client

        async def runme():
            return await client.create_playlist(name, item_ids=item_ids)

        def done(future):
            try:
                future.result()
            except Exception as e:
                log.warning("save queue as playlist failed: %s", e)
                app.show_toast(_("Couldn't save the playlist."))
                return
            app.show_toast(_("Saved “%s”") % name)
            if app.library is not None:
                GLib.idle_add(lambda: (app.library.refresh_playlists(), False)[1])

        app.runner.submit(runme()).add_done_callback(done)

    def _on_close_request(self, _window) -> bool:
        # Stop the player so the held GApplication releases and the process
        # actually exits when the window closes. Without this, hold() keeps
        # the app alive headlessly after X is pressed.
        if self.app.player:
            self.app.player.stop()
        return False

    def _save_geometry(self, *_args) -> None:
        s = self.app.settings
        if not self.is_maximized():
            w, h = self.get_default_size()
            s.set_int("window-width", w)
            s.set_int("window-height", h)
        s.set_boolean("window-maximized", self.is_maximized())
