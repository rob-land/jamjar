"""GStreamer playback engine — two `playbin3` decks with crossfade.

Two decks rather than one: a crossfade needs the outgoing and incoming
tracks audible at the same time, which a single `playbin3` can't do. The
decks alternate — the *active* deck is the one whose metadata, position
and state the rest of the app sees; the other is idle (NULL) except
during a crossfade, when it is the one fading out.

With `crossfade-seconds` at 0 the second deck is never used and playback
follows the original `about-to-finish` gapless path, unchanged. The same
path is also kept for consecutive tracks of one album, so albums stay
gapless even when crossfade is on (see `should_crossfade`).

Volume has two layers: a single user-facing *master* (what the volume
slider, MPRIS and the sleep timer manipulate) and a per-deck *gain* that
only the fade logic touches. The pipeline volume is the product.
"""

from __future__ import annotations

import logging
import math
import time
from pathlib import Path

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

# Fade timings. These are deliberately constants rather than settings —
# they exist to take the click off a manual transition, not as a taste
# knob. The user-tunable one is the crossfade length.
FADE_STEP_MS = 40
MANUAL_FADE_MS = 300
PAUSE_FADE_MS = 200
RESUME_FADE_MS = 200

MAX_CROSSFADE_SECONDS = 12


def equal_power(progress: float) -> tuple[float, float]:
    """(outgoing, incoming) gains at `progress` through a crossfade.

    A linear ramp dips at the midpoint — two uncorrelated signals at 0.5
    are audibly quieter than either at 1.0, because power adds, not
    amplitude. cos/sin keeps the sum of squares at 1 throughout, so the
    perceived loudness holds steady across the transition.
    """
    p = min(1.0, max(0.0, progress))
    angle = p * math.pi / 2
    return math.cos(angle), math.sin(angle)


def should_crossfade(current: Track | None, nxt: Track | None, *,
                     seconds: float, repeat: int,
                     album_crossfade: bool) -> bool:
    """Whether the transition from `current` to `nxt` should overlap."""
    if seconds <= 0 or nxt is None:
        return False
    if RepeatMode(repeat) is RepeatMode.ONE:
        # Overlapping a track with itself is a flanger, not a crossfade.
        return False
    if (not album_crossfade
            and current is not None
            and current.album_id
            and current.album_id == nxt.album_id):
        # Album sequencing is deliberate — leave it gapless.
        return False
    return True


class _Deck:
    """One `playbin3` plus the fade gain currently applied to it."""

    def __init__(self, name: str) -> None:
        self.pipeline = Gst.ElementFactory.make("playbin3", name)
        if self.pipeline is None:
            raise RuntimeError("playbin3 element not available — install gst-plugins-base")
        self.pipeline.set_property("flags", GST_PLAY_FLAG_AUDIO | GST_PLAY_FLAG_SOFT_VOLUME)
        self.gain: float = 1.0
        self.ramp_source: int | None = None
        self.bus = self.pipeline.get_bus()
        self.bus.add_signal_watch()


class Player(GObject.Object):
    """Wraps the decks and emits high-level signals for the UI."""

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

        self._decks = (_Deck("jamjar-deck-a"), _Deck("jamjar-deck-b"))
        self._active = 0
        self._master_volume = 1.0

        for deck in self._decks:
            deck.bus.connect("message::eos",           self._on_eos, deck)
            deck.bus.connect("message::error",         self._on_error, deck)
            deck.bus.connect("message::state-changed", self._on_state, deck)
            deck.bus.connect("message::async-done",     self._on_async_done, deck)
            # about-to-finish is a signal on playbin, not a bus message
            deck.pipeline.connect("about-to-finish", self._on_about_to_finish, deck)

        self.queue.connect("current-changed", self._on_queue_current_changed)

        self._codec = "copy"
        self._max_bitrate = 0
        self._replaygain = False
        self._crossfade_seconds = 0.0
        self._crossfade_albums = False
        self._duration_emitted: float = 0.0
        self._state = Gst.State.NULL
        # Set in about-to-finish when the next URI is queued; consumed on EOS.
        self._gapless_next: Track | None = None
        self._last_previous_at = 0.0
        self._tick_source: int | None = None
        # Crossfade bookkeeping
        self._crossfade_source: int | None = None
        self._crossfade_out: _Deck | None = None
        self._crossfade_step = 0
        self._crossfade_steps = 1
        # Seek to apply once a preloaded (restored) track has prerolled.
        self._pending_seek: float = 0.0
        # Set once a session exists; consulted before every stream URL so
        # a downloaded track plays from disk (and works with no network).
        self.offline = None
        # Seconds already skipped server-side on the current stream. Added
        # to the pipeline's own position so the UI sees absolute time.
        self._stream_offset = 0.0

    # ------- deck helpers -------

    @property
    def _deck(self) -> _Deck:
        return self._decks[self._active]

    @property
    def _idle(self) -> _Deck:
        return self._decks[1 - self._active]

    @property
    def _crossfading(self) -> bool:
        return self._crossfade_source is not None

    def _apply_gain(self, deck: _Deck) -> None:
        deck.pipeline.set_property("volume", self._master_volume * deck.gain)

    def _cancel_ramp(self, deck: _Deck) -> None:
        if deck.ramp_source is not None:
            GLib.source_remove(deck.ramp_source)
            deck.ramp_source = None

    def _ramp_gain(self, deck: _Deck, target: float, ms: int,
                   on_done=None) -> None:
        """Walk `deck.gain` to `target` over `ms`, then call `on_done`."""
        self._cancel_ramp(deck)
        start = deck.gain
        steps = max(1, ms // FADE_STEP_MS)
        counter = {"step": 0}

        def tick() -> bool:
            counter["step"] += 1
            progress = counter["step"] / steps
            if progress >= 1.0:
                deck.gain = target
                self._apply_gain(deck)
                deck.ramp_source = None
                if on_done is not None:
                    on_done()
                return False
            deck.gain = start + (target - start) * progress
            self._apply_gain(deck)
            return True

        deck.ramp_source = GLib.timeout_add(FADE_STEP_MS, tick)

    def _with_manual_fade(self, action) -> None:
        """Run `action` after fading the active deck out, if it's audible."""
        if self._state != Gst.State.PLAYING or self._deck.gain <= 0.0:
            action()
            return
        self._abort_crossfade()
        self._ramp_gain(self._deck, 0.0, MANUAL_FADE_MS, on_done=action)

    # ------- public API -------

    def configure(self, codec: str = "copy", max_bitrate: int = 0,
                  volume: float = 1.0, *, replaygain: bool = False,
                  crossfade_seconds: float = 0.0,
                  crossfade_albums: bool = False) -> None:
        self._codec = codec
        self._max_bitrate = max_bitrate
        self.set_volume(volume)
        self.set_replaygain(replaygain)
        self.set_crossfade(crossfade_seconds)
        self.set_crossfade_albums(crossfade_albums)

    def set_replaygain(self, enabled: bool) -> None:
        self._replaygain = enabled
        for deck in self._decks:
            if not enabled:
                deck.pipeline.set_property("audio-filter", None)
                continue
            # One element per pipeline — a GStreamer element belongs to a
            # single pipeline and can't be shared between the decks.
            filt = Gst.ElementFactory.make("rgvolume", None)
            if filt is None:
                log.warning("rgvolume element unavailable — ReplayGain disabled")
                return
            deck.pipeline.set_property("audio-filter", filt)

    def local_path(self, track: Track) -> str | None:
        """The downloaded file for `track`, if there is one."""
        if self.offline is None:
            return None
        path = self.offline.local_path(track.id)
        if path and Path(path).exists():
            return path
        return None

    def uri_for(self, track: Track, start_seconds: float = 0.0) -> str:
        """Local file if the track is downloaded, otherwise the stream.

        `start_seconds` is baked into a stream URL (server-side seek);
        local files ignore it because they seek properly.
        """
        path = self.local_path(track)
        if path is not None:
            self.offline.index.touch(track.id)
            return Gst.filename_to_uri(path)
        return self.queue.client.stream_url(
            track, codec=self._codec, max_bitrate=self._max_bitrate,
            start_seconds=start_seconds)

    def set_crossfade(self, seconds: float) -> None:
        self._crossfade_seconds = max(0.0, min(float(seconds), MAX_CROSSFADE_SECONDS))

    def set_crossfade_albums(self, enabled: bool) -> None:
        self._crossfade_albums = bool(enabled)

    def set_volume(self, value: float) -> None:
        self._master_volume = max(0.0, min(value, 1.0))
        for deck in self._decks:
            self._apply_gain(deck)

    @property
    def volume(self) -> float:
        return self._master_volume

    def play(self, track: Track | None = None) -> None:
        if track is None:
            track = self.queue.current
        if track is None:
            log.debug("play() with no current track")
            return
        self._abort_crossfade()
        url = self.uri_for(track)
        log.debug("playing %s -> %s", track.id, url)
        self._gapless_next = None
        self._pending_seek = 0.0
        self._stream_offset = 0.0
        deck = self._deck
        self._cancel_ramp(deck)
        deck.pipeline.set_state(Gst.State.NULL)
        deck.pipeline.set_property("uri", url)
        # Start silent and ramp in: a hard start on a track that begins
        # loud clicks, and this also covers the fade-out done by a skip.
        deck.gain = 0.0
        self._apply_gain(deck)
        deck.pipeline.set_state(Gst.State.PLAYING)
        self._ramp_gain(deck, 1.0, RESUME_FADE_MS)
        self._notify_track_started(track)

    def prepare(self, track: Track, position: float = 0.0) -> None:
        """Load `track` paused at `position` without starting playback.

        Used to restore the queue at startup: the bar and Now Playing
        page show what you were listening to, and pressing play picks up
        where you left off.
        """
        self._abort_crossfade()
        deck = self._deck
        self._cancel_ramp(deck)
        deck.pipeline.set_state(Gst.State.NULL)
        position = max(0.0, position)
        is_local = self.local_path(track) is not None
        # A local file seeks properly; a stream may not, so the offset
        # goes to the server instead.
        deck.pipeline.set_property(
            "uri", self.uri_for(track, 0.0 if is_local else position))
        deck.gain = 1.0
        self._apply_gain(deck)
        self._gapless_next = None
        self._stream_offset = 0.0 if is_local else position
        # A seek only sticks once the pipeline has prerolled, which is
        # what async-done announces.
        self._pending_seek = position if is_local else 0.0
        deck.pipeline.set_state(Gst.State.PAUSED)
        self._notify_track_started(track)
        if position:
            self.emit("position-changed", position)

    def _on_async_done(self, _bus, _msg, deck: _Deck) -> None:
        if self._pending_seek <= 0 or deck is not self._deck:
            return
        position = self._pending_seek
        self._pending_seek = 0.0
        deck.pipeline.seek_simple(
            Gst.Format.TIME,
            Gst.SeekFlags.FLUSH | Gst.SeekFlags.ACCURATE,
            int(position * Gst.SECOND),
        )
        self.emit("position-changed", position)

    def pause(self) -> None:
        self._abort_crossfade()
        deck = self._deck
        if self._state != Gst.State.PLAYING:
            deck.pipeline.set_state(Gst.State.PAUSED)
            return
        self._ramp_gain(deck, 0.0, PAUSE_FADE_MS,
                        on_done=lambda: deck.pipeline.set_state(Gst.State.PAUSED))

    def resume(self) -> None:
        deck = self._deck
        self._cancel_ramp(deck)
        deck.gain = 0.0
        self._apply_gain(deck)
        deck.pipeline.set_state(Gst.State.PLAYING)
        self._ramp_gain(deck, 1.0, RESUME_FADE_MS)

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
        self._abort_crossfade()
        self._gapless_next = None
        self._pending_seek = 0.0
        self._stream_offset = 0.0
        for deck in self._decks:
            self._cancel_ramp(deck)
            deck.pipeline.set_state(Gst.State.NULL)
            deck.gain = 1.0
            self._apply_gain(deck)
        self.emit("state-changed", "stopped")

    def close(self) -> None:
        """Release GStreamer and GLib resources owned by this player."""
        if self._tick_source is not None:
            GLib.source_remove(self._tick_source)
            self._tick_source = None
        self.stop()
        for deck in self._decks:
            deck.bus.remove_signal_watch()

    def next(self) -> None:
        self._last_previous_at = 0.0
        self._with_manual_fade(self._advance_or_stop)

    def _advance_or_stop(self) -> None:
        if self.queue.advance() is None:
            self.stop()

    def previous(self) -> None:
        now = time.monotonic()
        is_chained_press = now - self._last_previous_at <= PREVIOUS_CHAIN_SECONDS
        self._last_previous_at = now

        ok, pos = self._deck.pipeline.query_position(Gst.Format.TIME)
        if (not is_chained_press
                and ok
                and pos > RESTART_THRESHOLD_SECONDS * Gst.SECOND):
            # Restarting the current track isn't a transition — no fade.
            self.seek(0.0)
            return

        self._with_manual_fade(self.queue.previous)

    def seek(self, seconds: float) -> None:
        self._abort_crossfade()
        seconds = max(0.0, seconds)
        target = seconds - self._stream_offset
        seekable = self._deck.pipeline.seek_simple(
            Gst.Format.TIME,
            Gst.SeekFlags.FLUSH | Gst.SeekFlags.ACCURATE,
            int(max(0.0, target) * Gst.SECOND),
        ) if target >= 0 else False

        if not seekable:
            # Jellyfin behind a proxy answers with Accept-Ranges: none and
            # no Content-Length, and GStreamer cannot seek a stream it
            # cannot range-request. Ask the server to start the stream at
            # the target instead — the same trick the web client uses.
            self._restart_at(seconds)
            return

        # Push an immediate position update so the scrubber doesn't fight the
        # 500 ms poll until the user releases the slider.
        GLib.idle_add(self._emit_seek_position)

    def _restart_at(self, seconds: float) -> None:
        """Restart the current track from `seconds` using a server-side seek."""
        track = self.queue.current
        if track is None:
            return
        was_playing = self._state == Gst.State.PLAYING
        deck = self._deck
        self._cancel_ramp(deck)
        deck.pipeline.set_state(Gst.State.NULL)
        deck.pipeline.set_property("uri", self.uri_for(track, seconds))
        self._stream_offset = seconds
        deck.gain = 1.0
        self._apply_gain(deck)
        deck.pipeline.set_state(Gst.State.PLAYING if was_playing else Gst.State.PAUSED)
        log.debug("server-side seek to %.1fs on %s", seconds, track.id)
        self.emit("position-changed", seconds)

    def _emit_seek_position(self) -> bool:
        self.emit("position-changed", self.position)
        return False

    @property
    def position(self) -> float:
        ok, pos = self._deck.pipeline.query_position(Gst.Format.TIME)
        return (pos / Gst.SECOND if ok else 0.0) + self._stream_offset

    @property
    def duration(self) -> float:
        """Track length, from the pipeline or the item metadata.

        A Jellyfin stream behind a proxy that strips Content-Length has
        no queryable duration at all, so the metadata value is not a
        nicety — without it the crossfade would never know when the end
        was coming.
        """
        ok, dur = self._deck.pipeline.query_duration(Gst.Format.TIME)
        if ok and dur > 0:
            return dur / Gst.SECOND
        track = self.queue.current
        return track.duration_seconds if track is not None else 0.0

    @property
    def is_playing(self) -> bool:
        return self._state == Gst.State.PLAYING

    # ------- crossfade -------

    def _maybe_start_crossfade(self, position: float, duration: float) -> None:
        if self._crossfading or self._crossfade_seconds <= 0 or duration <= 0:
            return
        remaining = duration - position
        if remaining > self._crossfade_seconds:
            return
        nxt = self.queue.peek_next()
        if not should_crossfade(self.queue.current, nxt,
                                seconds=self._crossfade_seconds,
                                repeat=self.queue.repeat,
                                album_crossfade=self._crossfade_albums):
            return
        # Never run past the end of the outgoing track: the tick lands on a
        # 500 ms grid, so `remaining` is usually a little short of the
        # configured length.
        self._begin_crossfade(nxt, max(FADE_STEP_MS / 1000, min(self._crossfade_seconds, remaining)))

    def _begin_crossfade(self, nxt: Track, seconds: float) -> None:
        outgoing = self._deck
        incoming = self._idle
        self._cancel_ramp(outgoing)
        self._cancel_ramp(incoming)

        url = self.uri_for(nxt)
        incoming.pipeline.set_state(Gst.State.NULL)
        incoming.pipeline.set_property("uri", url)
        incoming.gain = 0.0
        self._apply_gain(incoming)
        incoming.pipeline.set_state(Gst.State.PLAYING)

        # The incoming deck becomes authoritative straight away: the new
        # track is what the user is starting to hear, so metadata, MPRIS
        # and the scrobbler flip now rather than when the fade ends.
        self._active = 1 - self._active
        self._gapless_next = None
        self._stream_offset = 0.0
        track = self.queue.advance(emit_current_changed=False)
        if track is None:
            # Queue moved underneath us — unwind and let EOS handle it.
            self._active = 1 - self._active
            incoming.pipeline.set_state(Gst.State.NULL)
            return

        log.debug("crossfading %.1fs into %s", seconds, track.id)
        self._crossfade_out = outgoing
        self._crossfade_step = 0
        self._crossfade_steps = max(1, int(seconds * 1000) // FADE_STEP_MS)
        self._crossfade_source = GLib.timeout_add(FADE_STEP_MS, self._on_crossfade_tick)

        self._notify_track_started(track)
        self.queue.emit("queue-changed")
        self._ensure_tick()

    def _on_crossfade_tick(self) -> bool:
        self._crossfade_step += 1
        progress = self._crossfade_step / self._crossfade_steps
        out_gain, in_gain = equal_power(progress)
        outgoing = self._crossfade_out
        incoming = self._deck
        if outgoing is not None:
            outgoing.gain = out_gain
            self._apply_gain(outgoing)
        incoming.gain = in_gain
        self._apply_gain(incoming)
        if progress >= 1.0:
            self._crossfade_source = None
            self._finish_crossfade()
            return False
        return True

    def _finish_crossfade(self) -> None:
        outgoing = self._crossfade_out
        self._crossfade_out = None
        if outgoing is not None:
            outgoing.pipeline.set_state(Gst.State.NULL)
            outgoing.gain = 1.0
            self._apply_gain(outgoing)
        self._deck.gain = 1.0
        self._apply_gain(self._deck)

    def _abort_crossfade(self) -> None:
        """Snap a crossfade to its end state — the queue already advanced."""
        if self._crossfade_source is not None:
            GLib.source_remove(self._crossfade_source)
            self._crossfade_source = None
            self._finish_crossfade()

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

    def _on_about_to_finish(self, _playbin, deck: _Deck) -> None:
        # Runs on a GStreamer streaming thread — only touch the pipeline URI
        # here; queue/UI sync happens on the main thread when EOS lands.
        # Reads of queue state (repeat, current, peek_next) are technically
        # unsynchronized, but under CPython's GIL individual attribute reads
        # are atomic. The worst case is reading a stale value; the GTK-thread
        # _complete_transition corrects any mismatch.
        if deck is not self._deck or self._crossfading:
            # The deck fading out during a crossfade also fires this; its
            # successor is already playing on the other deck.
            return
        if self.queue.repeat == RepeatMode.ONE and self.queue.current:
            self._gapless_next = self.queue.current
            deck.pipeline.set_property("uri", self.uri_for(self.queue.current))
            GLib.idle_add(self._complete_transition)
            return
        nxt = self.queue.peek_next()
        if nxt is None:
            return
        if should_crossfade(self.queue.current, nxt,
                            seconds=self._crossfade_seconds,
                            repeat=self.queue.repeat,
                            album_crossfade=self._crossfade_albums):
            # Handled by the crossfade tick on the other deck; priming this
            # one would start the next track twice.
            return
        self._gapless_next = nxt
        deck.pipeline.set_property("uri", self.uri_for(nxt))
        # EOS may not arrive for a gapless handoff; sync on the GTK thread
        # as soon as the next URI is queued.
        GLib.idle_add(self._complete_transition)

    def _complete_transition(self) -> bool:
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

        self._stream_offset = 0.0
        log.debug("gapless transition to %s", track.id)
        self._notify_track_started(track)
        self.queue.emit("queue-changed")
        return False

    def _on_eos(self, _bus, _msg, deck: _Deck) -> None:
        if deck is not self._deck:
            # The outgoing side of a crossfade reaching its end — the fade
            # tick owns the teardown.
            deck.pipeline.set_state(Gst.State.NULL)
            return

        # After about-to-finish the pipeline may already be playing the next
        # URI without another EOS for that handoff — consume the pending track
        # here so metadata and the scrubber stay in sync with the audio.
        if self._gapless_next is not None:
            self._complete_transition()
            return

        if self.queue.advance() is None:
            self.stop()

    def _on_error(self, _bus, msg, deck: _Deck) -> None:
        err, debug = msg.parse_error()
        log.error("gstreamer error on %s: %s (%s)",
                  deck.pipeline.get_name(), err.message, debug)
        if deck is not self._deck:
            # A failed crossfade prefetch shouldn't kill what's playing.
            self._abort_crossfade()
            deck.pipeline.set_state(Gst.State.NULL)
            return
        self.emit("error", err.message)
        self.stop()

    def _on_state(self, _bus, msg, deck: _Deck) -> None:
        if msg.src is not deck.pipeline or deck is not self._deck:
            return
        _old, new, _pending = msg.parse_state_changed()
        self._state = new
        if new == Gst.State.PLAYING:
            self._ensure_tick()
        elif self._tick_source is not None:
            GLib.source_remove(self._tick_source)
            self._tick_source = None
        names = {Gst.State.NULL: "stopped", Gst.State.READY: "ready",
                 Gst.State.PAUSED: "paused", Gst.State.PLAYING: "playing"}
        if new in names:
            self.emit("state-changed", names[new])

    def _ensure_tick(self) -> None:
        if self._tick_source is None:
            self._tick_source = GLib.timeout_add(500, self._tick)

    def _tick(self) -> bool:
        ok, _pos = self._deck.pipeline.query_position(Gst.Format.TIME)
        position = self.position
        if ok:
            self.emit("position-changed", position)
        duration = self.duration
        if duration and duration != self._duration_emitted:
            self._duration_emitted = duration
            self.emit("duration-changed", duration)
        self._maybe_start_crossfade(position, duration)
        return True
