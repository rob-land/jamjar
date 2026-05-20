"""Sleep timer — fades the player volume to silence after N minutes, then pauses.

The timer counts down independently of player state (a manual pause doesn't
freeze the countdown — that matches user expectation: "stop in 30 minutes"
means wall-clock 30 minutes, not 30 minutes of playback). On expiry the
volume tapers from the current level to 0 over `FADE_DURATION_S` seconds
in `FADE_STEP_MS` increments, then `Player.pause()` is called and the
original volume is restored so the next play session isn't silent.
"""

from __future__ import annotations

import logging

from gi.repository import GLib, GObject

log = logging.getLogger(__name__)

FADE_DURATION_S = 10
FADE_STEP_MS = 50


class SleepTimer(GObject.Object):
    __gtype_name__ = "JamjarSleepTimer"
    __gsignals__ = {
        # Fired when the timer starts, ticks (~once per second), and ends or
        # is cancelled. Argument is whole seconds remaining (0 when off).
        "remaining-changed": (GObject.SignalFlags.RUN_FIRST, None, (int,)),
    }

    def __init__(self) -> None:
        super().__init__()
        self.player = None
        self._expiry: float = 0.0  # monotonic seconds
        self._tick_source: int | None = None
        self._fade_source: int | None = None
        self._fade_step = 0
        self._fade_steps = 1
        self._fade_start_volume = 1.0
        self._restored_volume = 1.0

    def attach(self, player) -> None:
        self.player = player

    def detach(self) -> None:
        self.cancel()
        self.player = None

    # ------- state -------

    @property
    def is_active(self) -> bool:
        return self._tick_source is not None or self._fade_source is not None

    @property
    def remaining_seconds(self) -> int:
        if self._fade_source is not None:
            return 0
        if self._tick_source is None:
            return 0
        return max(0, int(self._expiry - GLib.get_monotonic_time() / 1_000_000))

    # ------- control -------

    def start(self, minutes: int) -> None:
        self.cancel()
        if minutes <= 0 or self.player is None:
            return
        self._expiry = GLib.get_monotonic_time() / 1_000_000 + minutes * 60
        self._tick_source = GLib.timeout_add_seconds(1, self._on_tick)
        self.emit("remaining-changed", minutes * 60)

    def cancel(self) -> None:
        if self._tick_source is not None:
            GLib.source_remove(self._tick_source)
            self._tick_source = None
        if self._fade_source is not None:
            GLib.source_remove(self._fade_source)
            self._fade_source = None
            # Mid-fade cancel: restore the volume the user had before the
            # fade started, otherwise the player would stay quiet.
            if self.player is not None:
                self.player.set_volume(self._restored_volume)
        self.emit("remaining-changed", 0)

    # ------- internals -------

    def _on_tick(self) -> bool:
        remaining = self.remaining_seconds
        if remaining <= 0:
            self._tick_source = None
            self._begin_fade()
            return False
        self.emit("remaining-changed", remaining)
        return True

    def _begin_fade(self) -> None:
        if self.player is None:
            self.emit("remaining-changed", 0)
            return
        self._restored_volume = self.player.volume
        self._fade_start_volume = self._restored_volume
        self._fade_step = 0
        self._fade_steps = max(1, (FADE_DURATION_S * 1000) // FADE_STEP_MS)
        self._fade_source = GLib.timeout_add(FADE_STEP_MS, self._on_fade_tick)

    def _on_fade_tick(self) -> bool:
        if self.player is None:
            self._fade_source = None
            self.emit("remaining-changed", 0)
            return False
        self._fade_step += 1
        progress = self._fade_step / self._fade_steps
        if progress >= 1.0:
            self.player.set_volume(0.0)
            self.player.pause()
            self.player.set_volume(self._restored_volume)
            self._fade_source = None
            self.emit("remaining-changed", 0)
            return False
        self.player.set_volume(self._fade_start_volume * (1.0 - progress))
        return True
