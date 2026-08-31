"""Right-click / long-press context menu for album tiles.

Mirrors the shape of `track_menu.install_track_menu`. The actions that
need the album's track list (Play Now, Play Next, Add to Queue) defer
until `Library.album_tracks(...)` returns — typically a single REST hit
for cold albums and instant for warm ones since the library caches the
per-album track list after first fetch.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING

from gi.repository import Gdk, Gio, GLib, Gtk

from ._common import commit_favorite, open_artist_by_id, start_instant_mix

if TYPE_CHECKING:
    from ..application import JamjarApplication
    from ..models import Album
    from ..window import JamjarWindow

log = logging.getLogger(__name__)


def install_album_menu(widget: Gtk.Widget,
                       get_album: Callable[[], Album | None],
                       app: JamjarApplication,
                       window: JamjarWindow) -> None:
    def show(x: float, y: float) -> None:
        album = get_album()
        if album is None:
            return
        popover = _build_popover(album, app, window, widget)
        popover.set_pointing_to(Gdk.Rectangle(x=int(x), y=int(y), width=1, height=1))
        popover.popup()

    rc = Gtk.GestureClick.new()
    rc.set_button(Gdk.BUTTON_SECONDARY)
    rc.connect("pressed", lambda _g, _n, x, y: show(x, y))
    widget.add_controller(rc)

    lp = Gtk.GestureLongPress.new()
    lp.connect("pressed", lambda _g, x, y: show(x, y))
    widget.add_controller(lp)


def _build_popover(album, app, window, parent) -> Gtk.PopoverMenu:
    is_fav = bool(album.user_data.get("IsFavorite"))
    fav_label = "Remove from Favorites" if is_fav else "Add to Favorites"

    ag = Gio.SimpleActionGroup.new()

    def add(name: str, fn, *, enabled: bool = True) -> None:
        a = Gio.SimpleAction.new(name, None)
        a.connect("activate", lambda *_: fn())
        a.set_enabled(enabled)
        ag.add_action(a)

    add("play-now",
        lambda: _with_tracks(album, app, lambda tracks: _play_now(tracks, app)),
        enabled=app.queue is not None and app.player is not None)
    add("play-next",
        lambda: _with_tracks(album, app,
                             lambda tracks: app.queue.play_next(tracks)),
        enabled=app.queue is not None)
    add("queue-add",
        lambda: _with_tracks(album, app,
                             lambda tracks: app.queue.append(tracks)),
        enabled=app.queue is not None)
    add("start-radio",
        lambda: start_instant_mix(album, app, kind="album"),
        enabled=(app.client is not None
                 and app.queue is not None
                 and app.player is not None))
    add("go-to-artist",
        lambda: _go_to_artist(album, app, window),
        enabled=bool(album.artist_ids))
    add("toggle-favorite",
        lambda: _toggle_favorite(album, app),
        enabled=app.client is not None)
    add("download",
        lambda: _with_tracks(album, app,
                             lambda tracks: _download(tracks, album, app)),
        enabled=app.offline is not None)

    menu = Gio.Menu()
    play_section = Gio.Menu()
    play_section.append("Play Now",     "albummenu.play-now")
    play_section.append("Play Next",    "albummenu.play-next")
    play_section.append("Add to Queue", "albummenu.queue-add")
    play_section.append("Start Album Radio", "albummenu.start-radio")
    menu.append_section(None, play_section)

    nav_section = Gio.Menu()
    nav_section.append("Go to Artist", "albummenu.go-to-artist")
    menu.append_section(None, nav_section)

    fav_section = Gio.Menu()
    fav_section.append(fav_label, "albummenu.toggle-favorite")
    fav_section.append("Download Album", "albummenu.download")
    menu.append_section(None, fav_section)

    popover = Gtk.PopoverMenu.new_from_model(menu)
    popover.insert_action_group("albummenu", ag)
    popover.set_parent(parent)
    popover.set_has_arrow(False)
    popover.connect("closed", lambda p: GLib.idle_add(p.unparent))
    return popover


def _with_tracks(album, app, callback) -> None:
    if app.library is None:
        return
    app.library.album_tracks(album.id, callback)


def _play_now(tracks: list, app) -> None:
    if not tracks or app.queue is None or app.player is None:
        return
    app.queue.replace(tracks, start_index=0)
    app.player.play(tracks[0])


def _go_to_artist(album, app, window) -> None:
    if not album.artist_ids:
        return
    open_artist_by_id(window, app, album.artist_ids[0])


def _toggle_favorite(album, app) -> None:
    if app.client is None:
        return
    new_state = not bool(album.user_data.get("IsFavorite"))
    commit_favorite(app.client, album, new_state, app.runner, app=app)


def _download(tracks, album, app) -> None:
    if app.offline is None or not tracks:
        return
    pending = [t for t in tracks if not app.offline.is_offline(t.id)]
    if not pending:
        app.show_toast(f"{album.name} is already downloaded")
        return
    app.offline.download(
        pending,
        on_done=lambda: app.show_toast(f"Downloaded {album.name}"),
    )
    app.show_toast(f"Downloading {album.name} ({len(pending)} tracks)…")
