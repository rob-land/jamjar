"""GStreamer playback engine wrapping playbin3."""

from __future__ import annotations

import logging
import time

from gi.repository import GLib, GObject, Gst

from .models import Track
from .queue import PlayQueue, RepeatMode

log = logging.getLogger(__name__)

Gst.init(None)

# playbin3 flag bits (from gst-plugins-base/playback)
GST_PLAY_FLAG_VIDEO       = 0x0001
GST_PLAY_FLAG_AUDIO       = 0x0002
GST_PLAY_FLAG_SOFT_VOLUME = 0x0010

PREVIOUS_CHAIN_SECONDS = 1.2
RESTART_THRESHOLD_SECONDS = 3.0


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

        self._bus = self.pipeline.get_bus()
        self._bus.add_signal_watch()
        self._bus.connect("message::eos",          self._on_eos)
        self._bus.connect("message::error",        self._on_error)
        self._bus.connect("message::state-changed", self._on_state)
        # about-to-finish is a signal on playbin, not a bus message
        self.pipeline.connect("about-to-finish", self._on_about_to_finish)

        self.queue.connect("current-changed", self._on_queue_current_changed)

        self._codec = "copy"
        self._max_bitrate = 0
        self._replaygain = False
        self._duration_emitted: float = 0.0
        self._state = Gst.State.NULL
        # Set in about-to-finish when the next URI is queued; consumed on EOS.
        self._gapless_next: Track | None = None
        self._last_previous_at = 0.0
        self._tick_source: int | None = None

    # ------- public API -------

    def configure(self, codec: str = "copy", max_bitrate: int = 0,
                  volume: float = 1.0, *, replaygain: bool = False) -> None:
        self._codec = codec
        self._max_bitrate = max_bitrate
        self.set_volume(volume)
        self.set_replaygain(replaygain)

    def set_replaygain(self, enabled: bool) -> None:
        self._replaygain = enabled
        if not enabled:
            self.pipeline.set_property("audio-filter", None)
            return
        filt = Gst.ElementFactory.make("rgvolume", "replaygain")
        if filt is None:
            log.warning("rgvolume element unavailable — ReplayGain disabled")
            return
        self.pipeline.set_property("audio-filter", filt)

    def set_volume(self, value: float) -> None:
        self.pipeline.set_property("volume", max(0.0, min(value, 1.0)))

    @property
    def volume(self) -> float:
        return float(self.pipeline.get_property("volume"))

    def play(self, track: Track | None = None) -> None:
        if track is None:
            track = self.queue.current
        if track is None:
            log.debug("play() with no current track")
            return
        url = self.queue.client.stream_url(
            track, codec=self._codec, max_bitrate=self._max_bitrate
        )
        log.debug("playing %s -> %s", track.id, url)
        self._gapless_next = None
        self.pipeline.set_state(Gst.State.NULL)
        self.pipeline.set_property("uri", url)
        self.pipeline.set_state(Gst.State.PLAYING)
        self._notify_track_started(track)

    def pause(self) -> None:
        self.pipeline.set_state(Gst.State.PAUSED)

    def resume(self) -> None:
        self.pipeline.set_state(Gst.State.PLAYING)

    def toggle(self) -> None:
        if self._state == Gst.State.PLAYING:
            self.pause()
        elif self._state in (Gst.State.PAUSED, Gst.State.READY, Gst.State.NULL):
            if self.queue.current is None:
                return
            if self._state == Gst.State.NULL:
                self.play(self.queue.current)
            else:
                self.resume()

    def stop(self) -> None:
        self._gapless_next = None
        self.pipeline.set_state(Gst.State.NULL)
        self.emit("state-changed", "stopped")

    def close(self) -> None:
        """Release GStreamer and GLib resources owned by this player."""
        if self._tick_source is not None:
            GLib.source_remove(self._tick_source)
            self._tick_source = None
        self.stop()
        self._bus.remove_signal_watch()

    def next(self) -> None:
        self._last_previous_at = 0.0
        if self.queue.advance() is None:
            self.stop()

    def previous(self) -> None:
        now = time.monotonic()
        is_chained_press = now - self._last_previous_at <= PREVIOUS_CHAIN_SECONDS
        self._last_previous_at = now

        ok, pos = self.pipeline.query_position(Gst.Format.TIME)
        if (not is_chained_press
                and ok
                and pos > RESTART_THRESHOLD_SECONDS * Gst.SECOND):
            self.seek(0.0)
            return

        self.queue.previous()

    def seek(self, seconds: float) -> None:
        ns = int(max(0.0, seconds) * Gst.SECOND)
        self.pipeline.seek_simple(
            Gst.Format.TIME,
            Gst.SeekFlags.FLUSH | Gst.SeekFlags.ACCURATE,
            ns,
        )
        # Push an immediate position update so the scrubber doesn't fight the
        # 500 ms poll until the user releases the slider.
        GLib.idle_add(self._emit_seek_position)

    def _emit_seek_position(self) -> bool:
        ok, pos = self.pipeline.query_position(Gst.Format.TIME)
        if ok:
            self.emit("position-changed", pos / Gst.SECOND)
        return False

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
        return self._state == Gst.State.PLAYING

    # ------- signal handlers -------

    def _on_queue_current_changed(self, queue, track) -> None:
        if track is None:
            self.stop()
        else:
            self.play(track)

    def _notify_track_started(self, track: Track) -> None:
        """Emit UI-facing signals when a new track becomes current."""
        self._duration_emitted = 0.0
        self.emit("track-changed", track)
        self.emit("position-changed", 0.0)
        if track.duration_seconds > 0:
            self.emit("duration-changed", track.duration_seconds)

    def _on_about_to_finish(self, _playbin) -> None:
        # Runs on a GStreamer streaming thread — only touch the pipeline URI
        # here; queue/UI sync happens on the main thread when EOS lands.
        # Reads of queue state (repeat, current, peek_next) are technically
        # unsynchronized, but under CPython's GIL individual attribute reads
        # are atomic. The worst case is reading a stale value; the GTK-thread
        # _complete_gapless_transition corrects any mismatch.
        if self.queue.repeat == RepeatMode.ONE and self.queue.current:
            self._gapless_next = self.queue.current
            url = self.queue.client.stream_url(
                self.queue.current, codec=self._codec, max_bitrate=self._max_bitrate
            )
            self.pipeline.set_property("uri", url)
            GLib.idle_add(self._complete_gapless_transition)
            return
        nxt = self.queue.peek_next()
        if nxt is not None:
            self._gapless_next = nxt
            url = self.queue.client.stream_url(
                nxt, codec=self._codec, max_bitrate=self._max_bitrate
            )
            self.pipeline.set_property("uri", url)
            # EOS may not arrive for a gapless handoff; sync on the GTK thread
            # as soon as the next URI is queued.
            GLib.idle_add(self._complete_gapless_transition)

    def _complete_gapless_transition(self) -> bool:
        """Sync queue index and UI after playbin starts the prefetched URI."""
        pending = self._gapless_next
        self._gapless_next = None
        if pending is None:
            return False

        if self.queue.repeat == RepeatMode.ONE:
            track = self.queue.current
        else:
            track = self.queue.advance(emit_current_changed=False)

        if track is None:
            self.stop()
            return False

        log.debug("gapless transition to %s", track.id)
        self._notify_track_started(track)
        self.queue.emit("queue-changed")
        return False

    def _on_eos(self, _bus, _msg) -> None:
        # After about-to-finish the pipeline may already be playing the next
        # URI without another EOS for that handoff — consume the pending track
        # here so metadata and the scrubber stay in sync with the audio.
        if self._gapless_next is not None:
            self._complete_gapless_transition()
            return

        if self.queue.advance() is None:
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
        self._state = new
        if new == Gst.State.PLAYING and self._tick_source is None:
            self._tick_source = GLib.timeout_add(500, self._tick)
        elif new != Gst.State.PLAYING and self._tick_source is not None:
            GLib.source_remove(self._tick_source)
            self._tick_source = None
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
