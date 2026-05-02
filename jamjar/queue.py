"""Play queue with shuffle and repeat modes."""

from __future__ import annotations

import logging
import random
from enum import IntEnum
from typing import Optional

import gi
from gi.repository import GObject

from .client import JellyfinClient
from .models import Track

log = logging.getLogger(__name__)


class RepeatMode(IntEnum):
    OFF = 0
    ALL = 1
    ONE = 2


class PlayQueue(GObject.Object):
    """Mutable list of tracks plus a play head."""

    __gtype_name__ = "JamjarPlayQueue"
    __gsignals__ = {
        "queue-changed":  (GObject.SignalFlags.RUN_FIRST, None, ()),
        "current-changed":(GObject.SignalFlags.RUN_FIRST, None, (object,)),
    }

    def __init__(self, client: JellyfinClient) -> None:
        super().__init__()
        self.client = client
        self._tracks: list[Track] = []
        self._index: int = -1
        self._shuffle: bool = False
        self._repeat: RepeatMode = RepeatMode.OFF

    # ------- properties -------

    @GObject.Property(type=bool, default=False)
    def shuffle(self) -> bool:
        return self._shuffle

    @shuffle.setter
    def shuffle(self, value: bool) -> None:
        if self._shuffle == value:
            return
        self._shuffle = bool(value)
        if self._shuffle:
            self._reshuffle_keep_current()
            self.emit("queue-changed")

    @GObject.Property(type=int, default=0)
    def repeat(self) -> int:
        return int(self._repeat)

    @repeat.setter
    def repeat(self, value: int) -> None:
        self._repeat = RepeatMode(value)

    @property
    def current(self) -> Optional[Track]:
        if 0 <= self._index < len(self._tracks):
            return self._tracks[self._index]
        return None

    @property
    def index(self) -> int:
        return self._index

    @property
    def tracks(self) -> list[Track]:
        return list(self._tracks)

    def __len__(self) -> int:
        return len(self._tracks)

    # ------- mutation -------

    def replace(self, tracks: list[Track], start_index: int = 0) -> None:
        self._tracks = list(tracks)
        self._index = start_index if 0 <= start_index < len(tracks) else (-1 if not tracks else 0)
        if self._shuffle:
            self._reshuffle_keep_current()
        self.emit("queue-changed")
        self.emit("current-changed", self.current)

    def append(self, tracks: list[Track]) -> None:
        if not tracks:
            return
        was_empty = not self._tracks
        self._tracks.extend(tracks)
        if was_empty:
            self._index = 0
            self.emit("current-changed", self.current)
        self.emit("queue-changed")

    def play_next(self, tracks: list[Track]) -> None:
        """Insert tracks immediately after the current item."""
        if not tracks:
            return
        insert_at = self._index + 1 if self._index >= 0 else 0
        self._tracks[insert_at:insert_at] = tracks
        if self._index < 0:
            self._index = 0
            self.emit("current-changed", self.current)
        self.emit("queue-changed")

    def remove(self, position: int) -> None:
        if not (0 <= position < len(self._tracks)):
            return
        del self._tracks[position]
        if position < self._index:
            self._index -= 1
        elif position == self._index:
            if self._index >= len(self._tracks):
                self._index = len(self._tracks) - 1
            self.emit("current-changed", self.current)
        self.emit("queue-changed")

    def move(self, src: int, dst: int) -> None:
        if not (0 <= src < len(self._tracks) and 0 <= dst < len(self._tracks)):
            return
        track = self._tracks.pop(src)
        self._tracks.insert(dst, track)
        if src == self._index:
            self._index = dst
        elif src < self._index <= dst:
            self._index -= 1
        elif dst <= self._index < src:
            self._index += 1
        self.emit("queue-changed")

    def clear(self) -> None:
        self._tracks.clear()
        self._index = -1
        self.emit("queue-changed")
        self.emit("current-changed", None)

    def jump_to(self, index: int) -> Optional[Track]:
        if not (0 <= index < len(self._tracks)):
            return None
        self._index = index
        self.emit("current-changed", self.current)
        return self.current

    # ------- navigation -------

    def peek_next(self) -> Optional[Track]:
        nxt = self._next_index(advance=False)
        return self._tracks[nxt] if nxt is not None else None

    def advance(self) -> Optional[Track]:
        nxt = self._next_index(advance=True)
        if nxt is None:
            self._index = -1
        else:
            self._index = nxt
        self.emit("current-changed", self.current)
        return self.current

    def previous(self) -> Optional[Track]:
        if self._repeat is RepeatMode.ONE and self._index >= 0:
            self.emit("current-changed", self.current)
            return self.current
        if self._index <= 0:
            if self._repeat is RepeatMode.ALL and self._tracks:
                self._index = len(self._tracks) - 1
            else:
                return self.current
        else:
            self._index -= 1
        self.emit("current-changed", self.current)
        return self.current

    def _next_index(self, advance: bool) -> Optional[int]:
        if not self._tracks:
            return None
        if self._repeat is RepeatMode.ONE and self._index >= 0:
            return self._index
        nxt = self._index + 1
        if nxt < len(self._tracks):
            return nxt
        if self._repeat is RepeatMode.ALL:
            return 0
        return None

    # ------- shuffle helpers -------

    def _reshuffle_keep_current(self) -> None:
        """Fisher–Yates over a copy, preserving the current track at index 0."""
        if not self._tracks:
            return
        rest = list(self._tracks)
        cur = rest.pop(self._index) if 0 <= self._index < len(rest) else None
        random.shuffle(rest)
        if cur is not None:
            self._tracks = [cur] + rest
            self._index = 0
        else:
            self._tracks = rest
