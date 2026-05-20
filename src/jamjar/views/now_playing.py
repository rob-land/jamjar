"""Now Playing surfaces — bottom bar + full-screen page."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from gi.repository import Adw, Gdk, GLib, GObject, Gtk

from ..lyrics import Lyrics, active_index, fetch_lyrics
from ..queue import RepeatMode
from ._common import (
    apply_favorite_visual,
    commit_favorite,
    format_duration,
    load_remote_image_async,
    make_link_label,
    open_album_by_id,
    open_artist_by_id,
)

if TYPE_CHECKING:
    from ..application import JamjarApplication
    from ..client import JellyfinClient
    from ..player import Player
    from ..queue import PlayQueue
    from ..window import JamjarWindow

log = logging.getLogger(__name__)


REPEAT_ICONS = {
    RepeatMode.OFF: ("media-playlist-repeat-symbolic",      "Repeat: Off"),
    RepeatMode.ALL: ("media-playlist-repeat-symbolic",      "Repeat: All"),
    RepeatMode.ONE: ("media-playlist-repeat-song-symbolic", "Repeat: One"),
}


def _next_repeat(mode: RepeatMode) -> RepeatMode:
    return RepeatMode((int(mode) + 1) % 3)


@Gtk.Template(resource_path="/land/rob/jamjar/now-playing-bar.ui")
class NowPlayingBar(Gtk.Box):
    __gtype_name__ = "JamjarNowPlayingBar"

    cover               = Gtk.Template.Child()
    text_box            = Gtk.Template.Child()
    title_label         = Gtk.Template.Child()
    artist_label        = Gtk.Template.Child()
    prev_button         = Gtk.Template.Child()
    play_pause_button   = Gtk.Template.Child()
    next_button         = Gtk.Template.Child()
    progress_scale      = Gtk.Template.Child()
    progress_adjustment = Gtk.Template.Child()
    position_label      = Gtk.Template.Child()
    duration_label      = Gtk.Template.Child()
    favorite_button     = Gtk.Template.Child()
    shuffle_button      = Gtk.Template.Child()
    repeat_button       = Gtk.Template.Child()
    volume_button       = Gtk.Template.Child()
    expand_button       = Gtk.Template.Child()

    compact = GObject.Property(type=bool, default=False)

    def __init__(self) -> None:
        super().__init__()
        self.player: Player | None = None
        self.queue: PlayQueue | None = None
        self.client: JellyfinClient | None = None
        self._suppress_seek = False
        self._suppress_favorite = False
        self._duration = 0.0
        self.expand_button.connect("clicked", self._on_expand)
        self.progress_scale.connect("change-value", self._on_seek)
        self.favorite_button.connect("toggled", self._on_favorite_toggled)
        self.shuffle_button.connect("toggled", self._on_shuffle_toggled)
        self.repeat_button.connect("clicked", self._on_repeat_clicked)
        self._wire_volume_button()
        # In compact mode the expand_button is hidden, so let a tap on the
        # cover/title area open the full Now Playing page instead.
        self._compact_tap = Gtk.GestureClick.new()
        self._compact_tap.connect("released", self._on_compact_tap)
        self.text_box.add_controller(self._compact_tap)
        self._cover_tap = Gtk.GestureClick.new()
        self._cover_tap.connect("released", self._on_compact_tap)
        self.cover.add_controller(self._cover_tap)
        self.connect("notify::compact", self._on_compact_changed)
        self._on_compact_changed()

    def attach(self, player, queue, client) -> None:
        self.player = player
        self.queue = queue
        self.client = client
        player.connect("track-changed",    self._on_track)
        player.connect("position-changed", self._on_position)
        player.connect("duration-changed", self._on_duration)
        player.connect("state-changed",    self._on_state)
        queue.connect("notify::shuffle",   lambda *_: self._sync_shuffle())
        queue.connect("notify::repeat",    lambda *_: self._sync_repeat())
        win = self.get_root()
        app = win.get_application() if win else None
        if app is not None:
            app.connect("favorite-changed", self._on_favorite_changed_external)
        # Pull current state so a freshly-built bar reflects what's playing.
        self._on_track(player, queue.current)
        self._on_state(player, "playing" if player.is_playing else "paused")
        self._sync_shuffle()
        self._sync_repeat()
        # Seed the volume slider/icon from the player (which application.py
        # already restored from GSettings).
        self._refresh_volume_icon(player.volume)

    def _on_compact_changed(self, *_):
        # In compact mode, strip down to cover + title + play/pause + next so the
        # bar fits a 360sp-wide phone (FLX1s portrait). Prev and the expand
        # button drop out; tap the cover/title area to open the Now Playing page.
        wide = not self.compact
        for w in (self.progress_scale, self.position_label, self.duration_label,
                  self.favorite_button, self.shuffle_button, self.repeat_button,
                  self.volume_button, self.prev_button, self.expand_button):
            w.set_visible(wide)
        # Title/artist column needs a generous min in desktop mode (so the
        # progress scale doesn't crush it) but can shrink freely on phone.
        self.text_box.set_size_request(120 if wide else 0, -1)

    def _on_track(self, _player, track) -> None:
        if track is None:
            self.title_label.set_label("")
            self.artist_label.set_label("")
            make_link_label(self.artist_label, None)
            self.cover.set_paintable(None)
            self._suppress_seek = True
            self.progress_adjustment.set_value(0.0)
            self._suppress_seek = False
            self.position_label.set_label("0:00")
            self.duration_label.set_label("0:00")
            self._sync_favorite(False)
            self.favorite_button.set_sensitive(False)
            return
        self._sync_favorite(bool(track.user_data.get("IsFavorite")))
        self.favorite_button.set_sensitive(True)
        self.title_label.set_label(track.name)
        self.artist_label.set_label(track.primary_artist)
        self._wire_artist_link(track)
        if (track.album_image_tag or track.image_tag) and self.client:
            url = self.client.cover_url(
                track.album_id or track.id,
                track.album_image_tag or track.image_tag,
                max_width=128,
            )
            load_remote_image_async(url, self.client.headers, self.cover,
                                    self.client.session,
                                    self.get_root().get_application().runner)
        # If we know the static duration from track metadata, prime the scale
        # so it isn't 0..1 until the pipeline catches up.
        if track.duration_seconds:
            self._duration = track.duration_seconds
            self.progress_adjustment.set_upper(track.duration_seconds)
            self.duration_label.set_label(format_duration(track.duration_seconds))

    def _wire_artist_link(self, track) -> None:
        win = self.get_root()
        app = win.get_application() if win else None
        if track.artist_ids and app is not None and win is not None:
            artist_id = track.artist_ids[0]
            make_link_label(self.artist_label,
                            lambda aid=artist_id: open_artist_by_id(win, app, aid))
        else:
            make_link_label(self.artist_label, None)

    def _on_position(self, _player, seconds: float) -> None:
        self._suppress_seek = True
        self.progress_adjustment.set_value(seconds)
        self._suppress_seek = False
        self.position_label.set_label(format_duration(seconds))

    def _on_duration(self, _player, seconds: float) -> None:
        if seconds <= 0:
            return
        self._duration = seconds
        self.progress_adjustment.set_upper(seconds)
        self.duration_label.set_label(format_duration(seconds))

    def _on_state(self, _player, state: str) -> None:
        icon = ("media-playback-pause-symbolic" if state == "playing"
                else "media-playback-start-symbolic")
        self.play_pause_button.set_icon_name(icon)

    def _on_seek(self, _scale, _scroll, value: float) -> bool:
        if self._suppress_seek or not self.player:
            return False
        self.player.seek(value)
        return False

    def _on_expand(self, _button) -> None:
        win = self.get_root()
        if hasattr(win, "show_now_playing"):
            win.show_now_playing()

    def _on_compact_tap(self, _gesture, _n_press, _x, _y) -> None:
        if not self.compact:
            return
        win = self.get_root()
        if hasattr(win, "show_now_playing"):
            win.show_now_playing()

    def _sync_favorite(self, is_favorite: bool) -> None:
        self._suppress_favorite = True
        self.favorite_button.set_active(is_favorite)
        self._suppress_favorite = False
        apply_favorite_visual(self.favorite_button, is_favorite)

    def _on_favorite_toggled(self, button) -> None:
        if self._suppress_favorite or self.queue is None or self.client is None:
            return
        track = self.queue.current
        if track is None:
            return
        new_state = button.get_active()
        apply_favorite_visual(button, new_state)
        app = self.get_root().get_application() if self.get_root() else None
        commit_favorite(self.client, track, new_state,
                         app.runner if app else None,
                         on_failure=lambda: self._sync_favorite(not new_state),
                         app=app)

    def _on_favorite_changed_external(self, _app, item_id: str, is_favorite: bool) -> None:
        # A favorite toggle landed somewhere else (row menu, page, etc.).
        # Sync the bar's heart if it matches the current track.
        if self.queue is None or self.queue.current is None:
            return
        if self.queue.current.id == item_id:
            self._sync_favorite(is_favorite)

    def _on_shuffle_toggled(self, button) -> None:
        if self.queue is None:
            return
        self.queue.shuffle = button.get_active()

    def _on_repeat_clicked(self, _button) -> None:
        if self.queue is None:
            return
        self.queue.repeat = int(_next_repeat(RepeatMode(self.queue.repeat)))

    def _sync_shuffle(self) -> None:
        if self.queue is None:
            return
        # Avoid signal recursion
        if self.shuffle_button.get_active() != self.queue.shuffle:
            self.shuffle_button.set_active(self.queue.shuffle)

    def _sync_repeat(self) -> None:
        if self.queue is None:
            return
        mode = RepeatMode(self.queue.repeat)
        icon, tip = REPEAT_ICONS[mode]
        self.repeat_button.set_icon_name(icon)
        self.repeat_button.set_tooltip_text(tip)
        # Use a CSS class for visual on-state when not OFF
        ctx = self.repeat_button.get_style_context()
        if mode == RepeatMode.OFF:
            ctx.remove_class("accent")
        else:
            ctx.add_class("accent")

    # ------- volume -------

    def _wire_volume_button(self) -> None:
        adj = Gtk.Adjustment.new(1.0, 0.0, 1.0, 0.05, 0.1, 0.0)
        scale = Gtk.Scale(orientation=Gtk.Orientation.VERTICAL,
                          adjustment=adj, draw_value=False, inverted=True,
                          height_request=140)
        scale.set_increments(0.05, 0.1)

        wrapper = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4,
                          margin_top=8, margin_bottom=8,
                          margin_start=6, margin_end=6, halign=Gtk.Align.CENTER)
        wrapper.append(scale)

        popover = Gtk.Popover()
        popover.set_child(wrapper)
        self.volume_button.set_popover(popover)

        self._volume_adjustment = adj
        self._volume_scale = scale
        adj.connect("value-changed", self._on_volume_changed)
        # Sync the slider to current volume each time the popover opens, in
        # case some other surface (MPRIS / future settings dialog) moved it.
        popover.connect("show", self._on_volume_popover_show)
        self._refresh_volume_icon(adj.get_value())

    def _on_volume_popover_show(self, _popover) -> None:
        if self.player is None:
            return
        self._volume_adjustment.set_value(self.player.volume)

    def _on_volume_changed(self, adj) -> None:
        v = adj.get_value()
        if self.player is not None:
            self.player.set_volume(v)
        app = self.get_root().get_application() if self.get_root() else None
        if app is not None:
            app.settings.set_double("volume", v)
        self._refresh_volume_icon(v)

    def _refresh_volume_icon(self, value: float) -> None:
        if value <= 0.001:
            icon = "audio-volume-muted-symbolic"
        elif value < 0.34:
            icon = "audio-volume-low-symbolic"
        elif value < 0.67:
            icon = "audio-volume-medium-symbolic"
        else:
            icon = "audio-volume-high-symbolic"
        self.volume_button.set_icon_name(icon)


@Gtk.Template(resource_path="/land/rob/jamjar/now-playing-page.ui")
class NowPlayingPage(Adw.NavigationPage):
    __gtype_name__ = "JamjarNowPlayingPage"

    sidebar_toggle       = Gtk.Template.Child()
    favorite_button      = Gtk.Template.Child()
    now_playing_carousel = Gtk.Template.Child()
    full_cover           = Gtk.Template.Child()
    lyrics_box           = Gtk.Template.Child()
    np_title    = Gtk.Template.Child()
    np_artist   = Gtk.Template.Child()
    np_album    = Gtk.Template.Child()
    np_progress = Gtk.Template.Child()
    np_position = Gtk.Template.Child()
    np_duration = Gtk.Template.Child()
    np_play     = Gtk.Template.Child()
    np_shuffle  = Gtk.Template.Child()
    np_repeat   = Gtk.Template.Child()

    def __init__(self, app: JamjarApplication, window: JamjarWindow) -> None:
        super().__init__()
        self.app = app
        self.window = window
        self._suppress_seek = False
        self._suppress_favorite = False
        self._duration = 0.0
        self._lyrics: Lyrics | None = None
        self._lyric_labels: list[Gtk.Label] = []
        self._lyrics_seq = 0
        self._active_lyric_index: int | None = None
        self._render_lyrics_placeholder("No track playing")
        self.sidebar_toggle.connect("clicked", lambda *_: window.toggle_sidebar())

        if app.player:
            app.player.connect("track-changed",    self._on_track)
            app.player.connect("position-changed", self._on_position)
            app.player.connect("duration-changed", self._on_duration)
            app.player.connect("state-changed",    self._on_state)
        if app.queue:
            app.queue.connect("notify::shuffle", lambda *_: self._sync_shuffle())
            app.queue.connect("notify::repeat",  lambda *_: self._sync_repeat())

        self.np_progress.connect("change-value", self._on_seek)
        self.np_shuffle.connect("toggled", self._on_shuffle_toggled)
        self.np_repeat.connect("clicked", self._on_repeat_clicked)
        self.favorite_button.connect("toggled", self._on_favorite_toggled)
        app.connect("favorite-changed", self._on_favorite_changed_external)

        # Prime initial state.
        if app.player and app.queue:
            self._on_track(app.player, app.queue.current)
            self._on_state(app.player, "playing" if app.player.is_playing else "paused")
            self._sync_shuffle()
            self._sync_repeat()

    def _on_track(self, _player, track) -> None:
        if track is None:
            self.np_title.set_label("")
            self.np_artist.set_label("")
            self.np_album.set_label("")
            make_link_label(self.np_artist, None)
            make_link_label(self.np_album, None)
            self.full_cover.set_paintable(None)
            self._sync_favorite(False)
            self.favorite_button.set_sensitive(False)
            self._render_lyrics_placeholder("No track playing")
            return
        self._sync_favorite(bool(track.user_data.get("IsFavorite")))
        self.favorite_button.set_sensitive(True)
        self.np_title.set_label(track.name)
        self.np_artist.set_label(track.primary_artist)
        self.np_album.set_label(track.album)
        if track.artist_ids:
            artist_id = track.artist_ids[0]
            make_link_label(self.np_artist,
                            lambda aid=artist_id: open_artist_by_id(self.window, self.app, aid))
        else:
            make_link_label(self.np_artist, None)
        if track.album_id:
            album_id = track.album_id
            make_link_label(self.np_album,
                            lambda aid=album_id: open_album_by_id(self.window, self.app, aid))
        else:
            make_link_label(self.np_album, None)
        self._fetch_lyrics(track)
        if (track.album_image_tag or track.image_tag) and self.app.client:
            url = self.app.client.cover_url(
                track.album_id or track.id,
                track.album_image_tag or track.image_tag,
                max_width=720,
            )
            load_remote_image_async(url, self.app.client.headers, self.full_cover,
                                    self.app.client.session, self.app.runner)
        if track.duration_seconds:
            self._duration = track.duration_seconds
            self.np_progress.get_adjustment().set_upper(track.duration_seconds)
            self.np_duration.set_label(format_duration(track.duration_seconds))

    def _on_position(self, _player, seconds: float) -> None:
        self._suppress_seek = True
        self.np_progress.get_adjustment().set_value(seconds)
        self._suppress_seek = False
        self.np_position.set_label(format_duration(seconds))
        self._update_lyric_highlight(seconds)

    def _on_duration(self, _player, seconds: float) -> None:
        if seconds <= 0:
            return
        self._duration = seconds
        self.np_progress.get_adjustment().set_upper(seconds)
        self.np_duration.set_label(format_duration(seconds))

    def _on_state(self, _player, state: str) -> None:
        icon = ("media-playback-pause-symbolic" if state == "playing"
                else "media-playback-start-symbolic")
        self.np_play.set_icon_name(icon)

    def _on_seek(self, _scale, _scroll, value: float) -> bool:
        if self._suppress_seek:
            return False
        self.app.player.seek(value)
        return False

    def _sync_favorite(self, is_favorite: bool) -> None:
        self._suppress_favorite = True
        self.favorite_button.set_active(is_favorite)
        self._suppress_favorite = False
        apply_favorite_visual(self.favorite_button, is_favorite)

    def _on_favorite_toggled(self, button) -> None:
        if (self._suppress_favorite or self.app.client is None
                or self.app.queue is None):
            return
        track = self.app.queue.current
        if track is None:
            return
        new_state = button.get_active()
        apply_favorite_visual(button, new_state)
        commit_favorite(self.app.client, track, new_state, self.app.runner,
                         on_failure=lambda: self._sync_favorite(not new_state),
                         app=self.app)

    def _on_favorite_changed_external(self, _app, item_id: str, is_favorite: bool) -> None:
        if self.app.queue is None or self.app.queue.current is None:
            return
        if self.app.queue.current.id == item_id:
            self._sync_favorite(is_favorite)

    # ------- lyrics -------

    def _clear_lyrics_box(self) -> None:
        for child in list(self.lyrics_box):
            self.lyrics_box.remove(child)
        self._lyric_labels = []
        self._active_lyric_index = None

    def _render_lyrics_placeholder(self, text: str) -> None:
        self._clear_lyrics_box()
        self._lyrics = None
        label = Gtk.Label(label=text, xalign=0.5, wrap=True)
        label.add_css_class("dim-label")
        self.lyrics_box.append(label)

    def _fetch_lyrics(self, track) -> None:
        if self.app.client is None:
            self._render_lyrics_placeholder("Not connected")
            return
        self._lyrics_seq += 1
        seq = self._lyrics_seq
        self._render_lyrics_placeholder("Loading lyrics…")

        async def runme():
            return await fetch_lyrics(self.app.client, track)

        def done(future):
            try:
                result = future.result()
            except Exception as e:
                log.warning("lyrics fetch failed: %s", e)
                result = None
            GLib.idle_add(lambda: (self._render_lyrics(seq, result), False)[1])

        self.app.runner.submit(runme()).add_done_callback(done)

    def _render_lyrics(self, seq: int, lyrics: Lyrics | None) -> None:
        # Drop stale callbacks: a faster, later track-change must not be
        # overwritten by a slower, earlier fetch.
        if seq != self._lyrics_seq:
            return
        if lyrics is None or not lyrics.lines:
            self._render_lyrics_placeholder("No lyrics available")
            return
        self._clear_lyrics_box()
        self._lyrics = lyrics
        for line in lyrics.lines:
            label = Gtk.Label(label=line.text or " ", xalign=0.5, wrap=True)
            label.add_css_class("lyrics-line")
            if line.seconds is not None:
                # Synced line: clickable to seek to the timestamp.
                label.add_css_class("lyrics-line-clickable")
                label.set_cursor(Gdk.Cursor.new_from_name("pointer", None))
                gesture = Gtk.GestureClick.new()
                gesture.connect(
                    "released",
                    lambda _g, _n, _x, _y, t=line.seconds: self._seek_to_lyric(t),
                )
                label.add_controller(gesture)
            self.lyrics_box.append(label)
            self._lyric_labels.append(label)
        # New lyrics: reset scroll to the top so the song starts from the
        # beginning of its lyrics, not wherever the previous song left off.
        sw = self.lyrics_box.get_parent()
        if isinstance(sw, Gtk.ScrolledWindow):
            sw.get_vadjustment().set_value(0)

    def _seek_to_lyric(self, seconds: float) -> None:
        if self.app.player is None:
            return
        self.app.player.seek(seconds)

    def _update_lyric_highlight(self, seconds: float) -> None:
        if self._lyrics is None or not self._lyrics.synced or not self._lyric_labels:
            return
        idx = active_index(self._lyrics, seconds)
        if idx == self._active_lyric_index:
            return
        if (self._active_lyric_index is not None
                and 0 <= self._active_lyric_index < len(self._lyric_labels)):
            self._lyric_labels[self._active_lyric_index].remove_css_class("active")
        self._active_lyric_index = idx
        if idx is not None and 0 <= idx < len(self._lyric_labels):
            self._lyric_labels[idx].add_css_class("active")
            self._scroll_active_into_view(idx)

    def _scroll_active_into_view(self, idx: int) -> None:
        label = self._lyric_labels[idx]
        sw = self.lyrics_box.get_parent()
        if not isinstance(sw, Gtk.ScrolledWindow):
            return

        # Defer to idle: on the first highlight after a track change the
        # newly-appended labels haven't been allocated yet, so their y/height
        # are still 0. By idle-time the layout pass has run.
        def do_scroll() -> bool:
            vadj = sw.get_vadjustment()
            alloc = label.get_allocation()
            if alloc.height <= 0:
                return False
            page = vadj.get_page_size()
            target = alloc.y + alloc.height / 2 - page / 2
            upper = max(0.0, vadj.get_upper() - page)
            target = max(0.0, min(target, upper))
            vadj.set_value(target)
            return False

        GLib.idle_add(do_scroll)

    def _on_shuffle_toggled(self, button) -> None:
        if self.app.queue is None:
            return
        self.app.queue.shuffle = button.get_active()

    def _on_repeat_clicked(self, _button) -> None:
        if self.app.queue is None:
            return
        self.app.queue.repeat = int(_next_repeat(RepeatMode(self.app.queue.repeat)))

    def _sync_shuffle(self) -> None:
        q = self.app.queue
        if q is None:
            return
        if self.np_shuffle.get_active() != q.shuffle:
            self.np_shuffle.set_active(q.shuffle)

    def _sync_repeat(self) -> None:
        q = self.app.queue
        if q is None:
            return
        mode = RepeatMode(q.repeat)
        icon, tip = REPEAT_ICONS[mode]
        self.np_repeat.set_icon_name(icon)
        self.np_repeat.set_tooltip_text(tip)
        ctx = self.np_repeat.get_style_context()
        if mode == RepeatMode.OFF:
            ctx.remove_class("accent")
        else:
            ctx.add_class("accent")
