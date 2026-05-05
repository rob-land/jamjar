"""Helpers shared across views."""

from __future__ import annotations

import logging
from typing import Callable, Optional

from gi.repository import Adw, Gdk, GdkPixbuf, GLib, Gio, Gtk

from .. import imagecache
from ..models import album_from_json, artist_from_json

log = logging.getLogger(__name__)


def format_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h:d}:{m:02d}:{s:02d}"
    return f"{m:d}:{s:02d}"


def escape_markup(text: Optional[str]) -> str:
    # Adw.ActionRow / Adw.PreferencesRow render title and subtitle as Pango
    # markup, so any server-supplied string with `&`, `<`, or `>` must be
    # escaped before going in.
    if not text:
        return ""
    return GLib.markup_escape_text(text)


def apply_favorite_visual(button: Gtk.Button, is_favorite: bool) -> None:
    if is_favorite:
        button.set_icon_name("emblem-favorite-symbolic")
        button.add_css_class("accent")
    else:
        button.set_icon_name("heart-outline-symbolic")
        button.remove_css_class("accent")


def favorite_heart(is_favorite: bool) -> Gtk.Image:
    """Build a small accent-colored heart suffix for an Adw.ActionRow.

    Visible only when `is_favorite` is True; toggle visibility (not the
    icon) to reflect state changes — that keeps the row layout stable
    instead of a heart-outline + heart-filled swap that nudges siblings.
    """
    img = Gtk.Image.new_from_icon_name("emblem-favorite-symbolic")
    img.add_css_class("accent")
    img.set_visible(bool(is_favorite))
    return img


def commit_favorite(client, item, new_state: bool, runner,
                    on_failure=None, app=None) -> None:
    """Fire the set_favorite REST call asynchronously for any item with `id`
    and `user_data` (Track/Album/Artist). On success, mutate
    `item.user_data['IsFavorite']` so other surfaces read the up-to-date
    state at next open, and emit `favorite-changed` on `app` if provided
    so live surfaces (row hearts, bar / now-playing toggles) sync without
    re-fetching. On failure, call `on_failure` from the GTK thread so the
    caller's toggle can revert.
    """
    async def runme():
        await client.set_favorite(item.id, new_state)

    def done(future):
        try:
            future.result()
        except Exception as e:
            log.warning("favorite toggle failed for %s: %s", item.id, e)
            if on_failure is not None:
                GLib.idle_add(lambda: (on_failure(), False)[1])
            return
        item.user_data["IsFavorite"] = new_state
        if app is not None:
            app.emit_favorite_changed(item.id, new_state)

    runner.submit(runme()).add_done_callback(done)


def fallback_icon(name: str = "audio-x-generic-symbolic", pixel_size: int = 64) -> Gtk.Image:
    img = Gtk.Image.new_from_icon_name(name)
    img.set_pixel_size(pixel_size)
    return img


def load_remote_image_async(url: str, headers: dict, picture: Gtk.Picture,
                            session, runner, fallback_icon_name: str = "audio-x-generic-symbolic") -> None:
    """Download an image to a Gdk.Texture and set it on `picture`. Best-effort.

    `session` is an aiohttp.ClientSession bound to `runner`.

    Stale-load guard: GridView / ColumnView recycle the same `Gtk.Picture`
    widget across many rows, so a single picture may receive several rebinds
    while previous fetches are still in flight. Without a guard, fetches
    settle in arbitrary order — leading to flicker and (when the last
    settler belongs to an earlier row) the wrong image. We stamp the
    target URL onto the picture and the apply step bails out unless the
    stamp still matches.

    On disk-cache hit the bytes are decoded and applied synchronously —
    the bind-time fast path that makes warm scrollback feel instant.
    """
    from aiohttp import ClientError

    # Stamp the picture with the URL we're targeting. If the picture was
    # already showing a different image, blank it so the user doesn't see
    # the previous row's artwork while ours loads. (For non-recycled
    # widgets the stamp just compares with itself — harmless.)
    prev_url = getattr(picture, "_jamjar_image_url", None)
    picture._jamjar_image_url = url
    if prev_url != url:
        picture.set_paintable(None)

    cached = imagecache.get(url)
    if cached is not None:
        _apply_image_bytes(picture, url, cached)
        return

    async def fetch():
        try:
            async with session.get(url, headers=headers) as r:
                r.raise_for_status()
                return await r.read()
        except ClientError as e:
            log.debug("image fetch failed: %s", e)
            return None

    def apply(future):
        try:
            data = future.result()
        except Exception as e:
            log.debug("image future failed: %s", e)
            data = None

        if data:
            imagecache.put(url, data)

        def set_on_main():
            # Bail if a later bind has retargeted the picture.
            if getattr(picture, "_jamjar_image_url", None) != url:
                return False
            if not data:
                picture.set_paintable(None)
                return False
            _set_pixbuf_from_bytes(picture, data)
            return False

        GLib.idle_add(set_on_main)

    runner.submit(fetch()).add_done_callback(apply)


def _apply_image_bytes(picture: Gtk.Picture, url: str, data: bytes) -> None:
    """Synchronous apply for the cache-hit fast path. Honours the URL stamp
    so a stale rebind that happens between the read and apply is still
    dropped (this should be rare since we're already on the GTK thread,
    but the check is cheap)."""
    if getattr(picture, "_jamjar_image_url", None) != url:
        return
    _set_pixbuf_from_bytes(picture, data)


def _set_pixbuf_from_bytes(picture: Gtk.Picture, data: bytes) -> None:
    try:
        loader = GdkPixbuf.PixbufLoader.new()
        loader.write(data)
        loader.close()
        pixbuf = loader.get_pixbuf()
        if pixbuf is not None:
            pixbuf = _fit_to_request(pixbuf, picture)
            picture.set_pixbuf(pixbuf)
    except GLib.Error as e:
        log.debug("image decode failed: %s", e.message)


def clear_remote_image(picture: Gtk.Picture) -> None:
    """Use this when binding a row whose item has no image — clears any
    paintable carried over from the previous row that recycled this widget,
    and drops the load stamp so any in-flight fetch is treated as stale.
    """
    picture._jamjar_image_url = None
    picture.set_paintable(None)


def _fit_to_request(pixbuf: GdkPixbuf.Pixbuf, picture: Gtk.Picture) -> GdkPixbuf.Pixbuf:
    # GtkPicture reports its loaded pixbuf's natural size to the layout, so
    # an oversize image (e.g., 256x256 cover into a 128x128 request) makes
    # the surrounding row grow. Scale the pixbuf down to ~2× the picture's
    # request (HiDPI headroom) so the picture's natural size matches the
    # tile size and the layout stays stable.
    req_w, req_h = picture.get_size_request()
    if req_w <= 0 or req_h <= 0:
        return pixbuf
    target_w, target_h = req_w * 2, req_h * 2
    if pixbuf.get_width() <= target_w and pixbuf.get_height() <= target_h:
        return pixbuf
    return pixbuf.scale_simple(target_w, target_h, GdkPixbuf.InterpType.BILINEAR)


# ------- navigation helpers (clickable artist/album labels) -------

def open_artist_by_id(window, app, artist_id: str) -> None:
    """Fetch an artist by id from Jellyfin and push the artist page."""
    if not artist_id or app.client is None:
        return

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


def open_album_by_id(window, app, album_id: str) -> None:
    """Fetch an album by id from Jellyfin and push the album page."""
    if not album_id or app.client is None:
        return

    async def fetch():
        return await app.client.get_item(album_id)

    def done(future):
        try:
            item = future.result()
        except Exception as e:
            log.warning("failed to fetch album %s: %s", album_id, e)
            return
        GLib.idle_add(lambda: (window.open_album(album_from_json(item)),
                               False)[1])

    app.runner.submit(fetch()).add_done_callback(done)


def make_link_label(label: Gtk.Label, target: Optional[Callable[[], None]]) -> None:
    """Wire `label` as a clickable link.

    `target` is invoked on click. Pass `None` to remove the link affordance
    (gesture stays attached but does nothing — re-call with a non-None
    target to re-enable). The first call attaches a single
    `Gtk.GestureClick`; later calls just swap the target, so this is safe
    to invoke on every rebind for recycled widgets.
    """
    label._jamjar_link_target = target
    if target is None:
        label.remove_css_class("link-label")
        label.set_cursor(None)
        return
    label.add_css_class("link-label")
    label.set_cursor(Gdk.Cursor.new_from_name("pointer", None))
    if getattr(label, "_jamjar_link_wired", False):
        return
    label._jamjar_link_wired = True

    def _on_release(_g, _n, _x, _y):
        cb = getattr(label, "_jamjar_link_target", None)
        if cb is not None:
            cb()

    gesture = Gtk.GestureClick.new()
    gesture.connect("released", _on_release)
    label.add_controller(gesture)
