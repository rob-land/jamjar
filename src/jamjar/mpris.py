"""MPRIS2 D-Bus service so media keys, GNOME Shell quick settings, and
Phosh lock-screen controls all "just work"."""

from __future__ import annotations

import logging
from typing import Optional

from .client import AsyncRunner
from .player import Player
from .queue import PlayQueue, RepeatMode

log = logging.getLogger(__name__)

BUS_NAME    = "org.mpris.MediaPlayer2.land.rob.Jamjar"
OBJECT_PATH = "/org/mpris/MediaPlayer2"


class MprisService:
    """Best-effort MPRIS2 implementation backed by `dbus-next`.

    Runs on the same async loop as the Jellyfin client. If `dbus-next` isn't
    available the service is inert (everything else still works).
    """

    def __init__(self, player: Player, queue: PlayQueue, runner: AsyncRunner,
                 on_quit=None, on_raise=None) -> None:
        self.player = player
        self.queue = queue
        self.runner = runner
        self.on_quit = on_quit
        self.on_raise = on_raise
        self._bus = None
        self._root_iface = None
        self._player_iface = None
        self._available = False

        try:
            self.runner.submit(self._setup()).result(timeout=4.0)
            self._available = True
        except Exception as e:
            log.warning("MPRIS service unavailable: %s", e)

        # Wire up player signals to MPRIS property changes
        player.connect("track-changed",    self._on_track)
        player.connect("state-changed",    self._on_state)
        player.connect("position-changed", self._on_position)

    async def _setup(self) -> None:
        from dbus_next.aio import MessageBus
        from dbus_next.service import ServiceInterface, method, dbus_property
        from dbus_next.constants import BusType, PropertyAccess
        from dbus_next.signature import Variant

        player = self.player
        queue = self.queue
        on_quit = self.on_quit
        on_raise = self.on_raise

        class Root(ServiceInterface):
            def __init__(self):
                super().__init__("org.mpris.MediaPlayer2")

            @method()
            def Raise(self):  # noqa: N802 (dbus naming)
                if on_raise:
                    on_raise()

            @method()
            def Quit(self):  # noqa: N802
                if on_quit:
                    on_quit()

            @dbus_property(access=PropertyAccess.READ)
            def CanRaise(self) -> "b":
                return on_raise is not None

            @dbus_property(access=PropertyAccess.READ)
            def CanQuit(self) -> "b":
                return on_quit is not None

            @dbus_property(access=PropertyAccess.READ)
            def HasTrackList(self) -> "b":
                return False

            @dbus_property(access=PropertyAccess.READ)
            def Identity(self) -> "s":
                return "Jamjar"

            @dbus_property(access=PropertyAccess.READ)
            def DesktopEntry(self) -> "s":
                return "land.rob.Jamjar"

            @dbus_property(access=PropertyAccess.READ)
            def SupportedUriSchemes(self) -> "as":
                return []

            @dbus_property(access=PropertyAccess.READ)
            def SupportedMimeTypes(self) -> "as":
                return []

        class PlayerIface(ServiceInterface):
            def __init__(self):
                super().__init__("org.mpris.MediaPlayer2.Player")

            @method()
            def Next(self):  # noqa: N802
                player.next()

            @method()
            def Previous(self):  # noqa: N802
                player.previous()

            @method()
            def Pause(self):  # noqa: N802
                player.pause()

            @method()
            def PlayPause(self):  # noqa: N802
                player.toggle()

            @method()
            def Stop(self):  # noqa: N802
                player.stop()

            @method()
            def Play(self):  # noqa: N802
                if player.queue.current is None and player.queue.tracks:
                    player.queue.jump_to(0)
                player.resume() if not player.is_playing else None

            @method()
            def Seek(self, offset: "x"):  # microseconds
                player.seek(player.position + offset / 1_000_000)

            @method()
            def SetPosition(self, _track_id: "o", position: "x"):
                player.seek(position / 1_000_000)

            @dbus_property(access=PropertyAccess.READ)
            def PlaybackStatus(self) -> "s":
                if player.is_playing:
                    return "Playing"
                if player.queue.current is not None:
                    return "Paused"
                return "Stopped"

            @dbus_property(access=PropertyAccess.READ)
            def LoopStatus(self) -> "s":
                return {RepeatMode.OFF: "None",
                        RepeatMode.ALL: "Playlist",
                        RepeatMode.ONE: "Track"}[RepeatMode(queue.repeat)]

            @dbus_property(access=PropertyAccess.READ)
            def Shuffle(self) -> "b":
                return queue.shuffle

            @dbus_property(access=PropertyAccess.READ)
            def Metadata(self) -> "a{sv}":
                track = queue.current
                if track is None:
                    return {}
                return {
                    "mpris:trackid":     Variant("o", f"/land/rob/Jamjar/track/{track.id}"),
                    "mpris:length":      Variant("x", int(track.duration_seconds * 1_000_000)),
                    "xesam:title":       Variant("s", track.name),
                    "xesam:album":       Variant("s", track.album),
                    "xesam:artist":      Variant("as", list(track.artists)),
                    "xesam:albumArtist": Variant("as", list(track.artists)),
                }

            @dbus_property(access=PropertyAccess.READ)
            def Volume(self) -> "d":
                return player.pipeline.get_property("volume")

            @dbus_property(access=PropertyAccess.READ)
            def Position(self) -> "x":
                return int(player.position * 1_000_000)

            @dbus_property(access=PropertyAccess.READ)
            def MinimumRate(self) -> "d":
                return 1.0

            @dbus_property(access=PropertyAccess.READ)
            def MaximumRate(self) -> "d":
                return 1.0

            @dbus_property(access=PropertyAccess.READ)
            def Rate(self) -> "d":
                return 1.0

            @dbus_property(access=PropertyAccess.READ)
            def CanGoNext(self) -> "b":
                return queue.peek_next() is not None

            @dbus_property(access=PropertyAccess.READ)
            def CanGoPrevious(self) -> "b":
                return queue.index > 0

            @dbus_property(access=PropertyAccess.READ)
            def CanPlay(self) -> "b":
                return queue.current is not None

            @dbus_property(access=PropertyAccess.READ)
            def CanPause(self) -> "b":
                return queue.current is not None

            @dbus_property(access=PropertyAccess.READ)
            def CanSeek(self) -> "b":
                return queue.current is not None

            @dbus_property(access=PropertyAccess.READ)
            def CanControl(self) -> "b":
                return True

        bus = await MessageBus(bus_type=BusType.SESSION).connect()
        root = Root()
        player_iface = PlayerIface()
        bus.export(OBJECT_PATH, root)
        bus.export(OBJECT_PATH, player_iface)
        await bus.request_name(BUS_NAME)
        self._bus = bus
        self._root_iface = root
        self._player_iface = player_iface

    # ------- property change emitters (best-effort) -------

    def _emit_changed(self, *prop_names: str) -> None:
        if not self._available or self._player_iface is None:
            return
        try:
            for name in prop_names:
                self._player_iface.emit_properties_changed({name: getattr(self._player_iface, name)})
        except Exception as e:
            log.debug("emit_properties_changed failed: %s", e)

    def _on_track(self, _player, _track) -> None:
        self._emit_changed("Metadata", "CanGoNext", "CanGoPrevious", "CanPlay")

    def _on_state(self, _player, _state) -> None:
        self._emit_changed("PlaybackStatus")

    def _on_position(self, _player, _seconds) -> None:
        # Don't emit on every tick; the spec says clients should infer position.
        pass
