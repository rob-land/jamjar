"""Right-click / long-press context menu for track rows.

`install_track_menu(widget, get_track, app, window)` attaches a
`Gtk.GestureClick` (right-click) and `Gtk.GestureLongPress` (touch) to
`widget` that pops up a menu with: Play Next, Add to Queue, Go to Album,
Go to Artist, and a Toggle Favorite item.

`get_track` is called each time the menu is opened so this helper works
on recycled widgets (GtkColumnView cells) where the bound track changes
over the widget's lifetime. For static rows pass `lambda t=track: t`.
"""

from __future__ import annotations

import logging
from typing import Callable, Optional, TYPE_CHECKING

import gi
gi.require_version("Gtk", "4.0")
from gi.repository import Gdk, Gio, GLib, Gtk

from ..models import album_from_json, artist_from_json
from ._common import commit_favorite

if TYPE_CHECKING:
    from ..application import JamjarApplication
    from ..models import Track
    from ..window import JamjarWindow

log = logging.getLogger(__name__)


def install_track_menu(widget: Gtk.Widget,
                       get_track: Callable[[], Optional["Track"]],
                       app: "JamjarApplication",
                       window: "JamjarWindow") -> None:
    def show(x: float, y: float) -> None:
        track = get_track()
        if track is None:
            return
        popover = _build_popover(track, app, window, widget)
        popover.set_pointing_to(Gdk.Rectangle(x=int(x), y=int(y),
                                              width=1, height=1))
        popover.popup()

    rc = Gtk.GestureClick.new()
    rc.set_button(Gdk.BUTTON_SECONDARY)
    rc.connect("pressed", lambda _g, _n, x, y: show(x, y))
    widget.add_controller(rc)

    lp = Gtk.GestureLongPress.new()
    lp.connect("pressed", lambda _g, x, y: show(x, y))
    widget.add_controller(lp)


def _build_popover(track, app, window, parent) -> Gtk.PopoverMenu:
    is_fav = bool(track.user_data.get("IsFavorite"))
    fav_label = "Remove from Favorites" if is_fav else "Add to Favorites"

    ag = Gio.SimpleActionGroup.new()

    def add_action(name: str, fn, *, enabled: bool = True) -> None:
        a = Gio.SimpleAction.new(name, None)
        a.connect("activate", lambda *_: fn())
        a.set_enabled(enabled)
        ag.add_action(a)

    add_action("play-now",
               lambda: _play_now(track, app),
               enabled=app.queue is not None and app.player is not None)
    add_action("play-next",
               lambda: app.queue and app.queue.play_next([track]),
               enabled=app.queue is not None)
    add_action("queue-add",
               lambda: app.queue and app.queue.append([track]),
               enabled=app.queue is not None)
    add_action("go-to-album",
               lambda: _go_to_album(track, app, window),
               enabled=bool(track.album_id))
    add_action("go-to-artist",
               lambda: _go_to_artist(track, app, window),
               enabled=bool(track.artist_ids))
    add_action("toggle-favorite",
               lambda: _toggle_favorite(track, app),
               enabled=app.client is not None)

    menu = Gio.Menu()
    play_section = Gio.Menu()
    play_section.append("Play Now", "trackmenu.play-now")
    play_section.append("Play Next", "trackmenu.play-next")
    play_section.append("Add to Queue", "trackmenu.queue-add")
    menu.append_section(None, play_section)

    nav_section = Gio.Menu()
    nav_section.append("Go to Album", "trackmenu.go-to-album")
    nav_section.append("Go to Artist", "trackmenu.go-to-artist")
    menu.append_section(None, nav_section)

    fav_section = Gio.Menu()
    fav_section.append(fav_label, "trackmenu.toggle-favorite")
    menu.append_section(None, fav_section)

    popover = Gtk.PopoverMenu.new_from_model(menu)
    popover.insert_action_group("trackmenu", ag)
    popover.set_parent(parent)
    popover.set_has_arrow(False)
    # Detach from the parent widget once dismissed; otherwise repeated
    # right-clicks accumulate orphaned popovers under the row.
    popover.connect("closed", lambda p: GLib.idle_add(p.unparent))
    return popover


def _go_to_album(track, app, window) -> None:
    if not track.album_id or app.client is None:
        return

    async def fetch():
        return await app.client.get_item(track.album_id)

    def done(future):
        try:
            item = future.result()
        except Exception as e:
            log.warning("failed to fetch album %s: %s", track.album_id, e)
            return
        GLib.idle_add(lambda: (window.open_album(album_from_json(item)),
                               False)[1])

    app.runner.submit(fetch()).add_done_callback(done)


def _go_to_artist(track, app, window) -> None:
    if not track.artist_ids or app.client is None:
        return
    artist_id = track.artist_ids[0]

    async def fetch():
        return await app.client.get_item(artist_id)

    def done(future):
        try:
            item = future.result()
        except Exception as e:
            log.warning("failed to fetch artist %s: %s", artist_id, e)
            return
        GLib.idle_add(lambda: (window.open_artist(artist_from_json(item)),
                               False)[1])

    app.runner.submit(fetch()).add_done_callback(done)


def _toggle_favorite(track, app) -> None:
    if app.client is None:
        return
    new_state = not bool(track.user_data.get("IsFavorite"))
    commit_favorite(app.client, track, new_state, app.runner)


def _play_now(track, app) -> None:
    if app.queue is None or app.player is None:
        return
    app.queue.replace([track], start_index=0)
    app.player.play(track)
