"""GStreamer playback engine wrapping playbin3."""

from __future__ import annotations

import logging
from typing import Optional

import gi
gi.require_version("Gst", "1.0")
from gi.repository import GLib, GObject, Gst

from .models import Track
from .queue import PlayQueue, RepeatMode

log = logging.getLogger(__name__)

Gst.init(None)

# playbin3 flag bits (from gst-plugins-base/playback)
GST_PLAY_FLAG_VIDEO       = 0x0001
GST_PLAY_FLAG_AUDIO       = 0x0002
GST_PLAY_FLAG_SOFT_VOLUME = 0x0010


class Player(GObject.Object):
    """Wraps a `playbin3` pipeline and emits high-level signals.

    Always set the next URI inside the `about-to-finish` callback for gapless
    playback — that's the playbin3 contract.
    """

    __gtype_name__ = "JamjarPlayer"
    __gsignals__ = {
        "position-changed": (GObject.SignalFlags.RUN_FIRST, None, (float,)),
        "duration-changed": (GObject.SignalFlags.RUN_FIRST, None, (float,)),
        "track-changed":    (GObject.SignalFlags.RUN_FIRST, None, (object,)),
        "state-changed":    (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        "error":            (GObject.SignalFlags.RUN_FIRST, None, (str,)),
    }

    def __init__(self, queue: PlayQueue) -> None:
        super().__init__()
        self.queue = queue

        self.pipeline = Gst.ElementFactory.make("playbin3", "jamjar-player")
        if self.pipeline is None:
            raise RuntimeError("playbin3 element not available — install gst-plugins-base")

        self.pipeline.set_property("flags", GST_PLAY_FLAG_AUDIO | GST_PLAY_FLAG_SOFT_VOLUME)
        self.pipeline.set_property("volume", 1.0)

        bus = self.pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message::eos",          self._on_eos)
        bus.connect("message::error",        self._on_error)
        bus.connect("message::state-changed", self._on_state)
        # about-to-finish is a signal on playbin, not a bus message
        self.pipeline.connect("about-to-finish", self._on_about_to_finish)

        self.queue.connect("current-changed", self._on_queue_current_changed)

        self._codec = "copy"
        self._max_bitrate = 0
        self._duration_emitted: float = 0.0
        GLib.timeout_add(500, self._tick)

    # ------- public API -------

    def configure(self, codec: str = "copy", max_bitrate: int = 0,
                  volume: float = 1.0) -> None:
        self._codec = codec
        self._max_bitrate = max_bitrate
        self.set_volume(volume)

    def set_volume(self, value: float) -> None:
        self.pipeline.set_property("volume", max(0.0, min(value, 1.0)))

    @property
    def volume(self) -> float:
        return float(self.pipeline.get_property("volume"))

    def play(self, track: Optional[Track] = None) -> None:
        if track is None:
            track = self.queue.current
        if track is None:
            log.debug("play() with no current track")
            return
        url = self.queue.client.stream_url(
            track, codec=self._codec, max_bitrate=self._max_bitrate
        )
        log.debug("playing %s -> %s", track.id, url)
        self.pipeline.set_state(Gst.State.NULL)
        self.pipeline.set_property("uri", url)
        self.pipeline.set_state(Gst.State.PLAYING)
        self.emit("track-changed", track)

    def pause(self) -> None:
        self.pipeline.set_state(Gst.State.PAUSED)

    def resume(self) -> None:
        self.pipeline.set_state(Gst.State.PLAYING)

    def toggle(self) -> None:
        ok, state, _ = self.pipeline.get_state(Gst.CLOCK_TIME_NONE)
        if state == Gst.State.PLAYING:
            self.pause()
        elif state in (Gst.State.PAUSED, Gst.State.READY, Gst.State.NULL):
            if self.queue.current is None:
                return
            if state == Gst.State.NULL:
                self.play(self.queue.current)
            else:
                self.resume()

    def stop(self) -> None:
        self.pipeline.set_state(Gst.State.NULL)
        self.emit("state-changed", "stopped")

    def next(self) -> None:
        if self.queue.advance() is None:
            self.stop()

    def previous(self) -> None:
        ok, pos = self.pipeline.query_position(Gst.Format.TIME)
        if ok and pos > 3 * Gst.SECOND:
            self.seek(0.0)
            return
        if self.queue.previous() is not None:
            self.play(self.queue.current)

    def seek(self, seconds: float) -> None:
        ns = int(max(0.0, seconds) * Gst.SECOND)
        self.pipeline.seek_simple(
            Gst.Format.TIME,
            Gst.SeekFlags.FLUSH | Gst.SeekFlags.KEY_UNIT,
            ns,
        )

    @property
    def position(self) -> float:
        ok, pos = self.pipeline.query_position(Gst.Format.TIME)
        return pos / Gst.SECOND if ok else 0.0

    @property
    def duration(self) -> float:
        ok, dur = self.pipeline.query_duration(Gst.Format.TIME)
        return dur / Gst.SECOND if ok else 0.0

    @property
    def is_playing(self) -> bool:
        ok, state, _ = self.pipeline.get_state(0)
        return state == Gst.State.PLAYING

    # ------- signal handlers -------

    def _on_queue_current_changed(self, queue, track) -> None:
        if track is None:
            self.stop()
        else:
            self.play(track)

    def _on_about_to_finish(self, _playbin) -> None:
        # Special: this runs on a streaming thread. Setting the URI directly is
        # the documented gapless pattern; the pipeline picks it up at EOS.
        if self.queue.repeat == RepeatMode.ONE and self.queue.current:
            url = self.queue.client.stream_url(
                self.queue.current, codec=self._codec, max_bitrate=self._max_bitrate
            )
            self.pipeline.set_property("uri", url)
            return
        nxt = self.queue.peek_next()
        if nxt is not None:
            url = self.queue.client.stream_url(
                nxt, codec=self._codec, max_bitrate=self._max_bitrate
            )
            self.pipeline.set_property("uri", url)

    def _on_eos(self, _bus, _msg) -> None:
        # playbin3 fires EOS after about-to-finish even when we set a new URI;
        # advance the queue index to keep state consistent.
        new = self.queue.advance()
        if new is not None:
            self.emit("track-changed", new)
        else:
            self.stop()

    def _on_error(self, _bus, msg) -> None:
        err, debug = msg.parse_error()
        log.error("gstreamer error: %s (%s)", err.message, debug)
        self.emit("error", err.message)
        self.stop()

    def _on_state(self, _bus, msg) -> None:
        if msg.src is not self.pipeline:
            return
        old, new, _pending = msg.parse_state_changed()
        names = {Gst.State.NULL: "stopped", Gst.State.READY: "ready",
                 Gst.State.PAUSED: "paused", Gst.State.PLAYING: "playing"}
        if new in names:
            self.emit("state-changed", names[new])

    def _tick(self) -> bool:
        ok, pos = self.pipeline.query_position(Gst.Format.TIME)
        if ok:
            self.emit("position-changed", pos / Gst.SECOND)
        ok, dur = self.pipeline.query_duration(Gst.Format.TIME)
        if ok:
            duration = dur / Gst.SECOND
            if duration != self._duration_emitted:
                self._duration_emitted = duration
                self.emit("duration-changed", duration)
        return True
