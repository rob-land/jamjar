"""Reports playback state to Jellyfin via /Sessions/Playing endpoints."""

from __future__ import annotations

import logging

from gi.repository import GLib, GObject

from .client import AsyncRunner, JellyfinClient
from .models import Track
from .player import Player
from .queue import PlayQueue

log = logging.getLogger(__name__)


class Scrobbler(GObject.Object):
    """Wires Player + Queue events to Jellyfin's playback reporting endpoints.

    POST /Sessions/Playing on track start, /Sessions/Playing/Progress every 10s,
    /Sessions/Playing/Stopped on stop. Position is in ticks (1 tick = 100 ns).
    """

    __gtype_name__ = "JamjarScrobbler"

    PROGRESS_INTERVAL_MS = 10_000

    def __init__(self, client: JellyfinClient, player: Player,
                 queue: PlayQueue, runner: AsyncRunner) -> None:
        super().__init__()
        self.client = client
        self.player = player
        self.queue = queue
        self.runner = runner

        self._current: Track | None = None
        self._position_seconds: float = 0.0
        self._is_paused: bool = False
        self._progress_source: int | None = None

        player.connect("track-changed",    self._on_track_changed)
        player.connect("state-changed",    self._on_state_changed)
        player.connect("position-changed", self._on_position_changed)

    # ------- handlers -------

    def _on_track_changed(self, _player, track: Track | None) -> None:
        if self._current and self._current.id != (track.id if track else None):
            self._send_stopped()
        self._current = track
        if track is not None:
            self._send_playing(track)
            self._start_progress_timer()
        else:
            self._stop_progress_timer()

    def _on_state_changed(self, _player, state: str) -> None:
        was_paused = self._is_paused
        self._is_paused = (state == "paused")
        if state == "stopped":
            self._send_stopped()
            self._stop_progress_timer()
            self._current = None
            return
        # Send an immediate progress report on a pause↔play flip so MPRIS
        # observers (GNOME Shell quick settings, Phosh lockscreen, etc.)
        # and the Jellyfin dashboard learn the new state without waiting
        # up to PROGRESS_INTERVAL_MS for the next tick. Filter to actual
        # transitions so the NULL→READY→PAUSED→PLAYING ramp-up at track
        # start doesn't fan out multiple "playing" reports.
        if state in ("playing", "paused") \
                and was_paused != self._is_paused \
                and self._current is not None:
            self._send_progress()

    def _on_position_changed(self, _player, seconds: float) -> None:
        self._position_seconds = seconds

    # ------- timer -------

    def _start_progress_timer(self) -> None:
        self._stop_progress_timer()
        self._progress_source = GLib.timeout_add(
            self.PROGRESS_INTERVAL_MS, self._tick_progress
        )

    def _stop_progress_timer(self) -> None:
        if self._progress_source is not None:
            GLib.source_remove(self._progress_source)
            self._progress_source = None

    def _tick_progress(self) -> bool:
        if self._current is None:
            return False
        self._send_progress()
        return True

    # ------- requests -------

    def _ticks(self, seconds: float) -> int:
        return int(seconds * 10_000_000)

    def _base_body(self) -> dict:
        return {
            "ItemId":       self._current.id,
            "PositionTicks": self._ticks(self._position_seconds),
            "IsPaused":      self._is_paused,
            "PlayMethod":    "Transcode",
            "MediaSourceId": self._current.id,
        }

    def _send_playing(self, track: Track) -> None:
        body = {
            "ItemId":      track.id,
            "MediaSourceId": track.id,
            "PlayMethod":  "Transcode",
            "PositionTicks": 0,
        }
        self.runner.submit(self.client.report_playing(body))

    def _send_progress(self) -> None:
        if self._current is None:
            return
        self.runner.submit(self.client.report_progress(self._base_body()))

    def _send_stopped(self) -> None:
        if self._current is None:
            return
        self.runner.submit(self.client.report_stopped(self._base_body()))
