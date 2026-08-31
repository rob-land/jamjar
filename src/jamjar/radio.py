"""Radio stations — endless queues built from library metadata.

Jellyfin has no sonic analysis, so there is no "mood" the way Plex has
one. What it does expose is genres, production years, user tags,
favorites, and `/Items/{id}/InstantMix`. Stations here are built from
exactly that:

  * **style**     — one genre
  * **decade**    — the ten years of a decade present in the library
  * **mood**      — a curated genre grouping (`MOOD_GENRES`), narrowed to
                    the genres the library actually has
  * **tag**       — a tag the user put on their own files
  * **favorites** / **surprise** — always available
  * **mix**       — Jellyfin's Instant Mix seeded from a track, album or
                    artist (what the context-menu radio entries use)

A station is *endless*: `RadioSession` refills the queue with a fresh
batch whenever it runs low, so it behaves like a radio rather than a
one-shot 100-track playlist.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from gi.repository import GLib, GObject

log = logging.getLogger(__name__)

BATCH_SIZE = 60
REFILL_THRESHOLD = 5
# Cap on remembered ids: a long session shouldn't grow the exclude set
# without bound, and a track heard two hours ago is fair game again.
SERVED_MEMORY = 500

# Mood → genres. Intersected with the library's real genres at build time,
# so a mood with nothing behind it never appears.
MOOD_GENRES: dict[str, tuple[str, ...]] = {
    "Chill":      ("Ambient", "Downtempo", "Chillout", "Trip-Hop", "Folk", "Jazz"),
    "Energetic":  ("Punk", "Dance", "House", "Techno", "Metal", "Hardcore", "Drum & Bass"),
    "Focus":      ("Classical", "Post-Rock", "Minimal", "Instrumental", "Soundtrack"),
    "Late Night": ("Soul", "R&B", "Blues", "Lounge", "Jazz"),
    "Guitars":    ("Rock", "Indie", "Grunge", "Alternative", "Blues Rock"),
    "Dancefloor": ("Disco", "Funk", "House", "Electronic", "Pop"),
}

# Names verified against Adwaita: an icon the theme doesn't have renders as
# a broken-image placeholder rather than falling back to something sensible.
MOOD_ICONS: dict[str, str] = {
    "Chill":      "weather-clear-night-symbolic",
    "Energetic":  "power-profile-performance-symbolic",
    "Focus":      "document-edit-symbolic",
    "Late Night": "night-light-symbolic",
    "Guitars":    "audio-headphones-symbolic",
    "Dancefloor": "audio-speakers-symbolic",
}

DEFAULT_ICON = "audio-x-generic-symbolic"


@dataclass(frozen=True)
class Station:
    """A description of what to ask Jellyfin for. No state, no I/O."""

    key: str
    title: str
    kind: str
    subtitle: str = ""
    icon: str = DEFAULT_ICON
    genres: tuple[str, ...] = ()
    years: tuple[int, ...] = ()
    tags: tuple[str, ...] = ()
    favorites: bool = False
    seed_id: str = ""


def _decades(years: list[int]) -> list[int]:
    return sorted({(y // 10) * 10 for y in years if y >= 1900}, reverse=True)


def build_stations(genres: list[str], years: list[int],
                   tags: list[str]) -> list[Station]:
    """Turn a library's filter vocabulary into the stations to offer.

    Pure — everything it needs comes from `client.item_filters` and
    `client.item_tags`, which makes the whole picker testable without a
    server.
    """
    available = {g.casefold(): g for g in genres if g}
    stations: list[Station] = [
        Station(key="favorites", title="Favorites", kind="favorites",
                subtitle="Tracks you've hearted",
                icon="emblem-favorite-symbolic", favorites=True),
        Station(key="surprise", title="Surprise Me", kind="surprise",
                subtitle="Anything from your library",
                icon="media-playlist-shuffle-symbolic"),
    ]

    for mood, wanted in MOOD_GENRES.items():
        matched = tuple(available[w.casefold()] for w in wanted
                        if w.casefold() in available)
        if not matched:
            continue
        stations.append(Station(
            key=f"mood:{mood.casefold()}", title=mood, kind="mood",
            subtitle=", ".join(matched[:3]),
            icon=MOOD_ICONS.get(mood, DEFAULT_ICON),
            genres=matched,
        ))

    for decade in _decades(years):
        stations.append(Station(
            key=f"decade:{decade}", title=f"{decade}s", kind="decade",
            subtitle=f"{decade}–{decade + 9}",
            icon="document-open-recent-symbolic",
            years=tuple(range(decade, decade + 10)),
        ))

    for genre in genres:
        if not genre:
            continue
        stations.append(Station(
            key=f"style:{genre.casefold()}", title=genre, kind="style",
            icon=DEFAULT_ICON, genres=(genre,),
        ))

    # A tag that just repeats a genre or mood name would be a duplicate
    # card doing the same thing by a different query.
    taken = {s.title.casefold() for s in stations}
    for tag in tags:
        if not tag or tag.casefold() in taken:
            continue
        stations.append(Station(
            key=f"tag:{tag.casefold()}", title=tag, kind="tag",
            icon="tag-symbolic", tags=(tag,),
        ))

    return stations


def mix_station(item, *, kind_label: str) -> Station:
    """A Station wrapping Jellyfin's Instant Mix seeded from `item`."""
    return Station(
        key=f"mix:{item.id}", title=getattr(item, "name", "Radio"), kind="mix",
        subtitle=f"Similar to this {kind_label}",
        icon="media-playlist-shuffle-symbolic", seed_id=item.id,
    )


class RadioSession(GObject.Object):
    """Owns the running station and keeps its queue topped up.

    Ownership is tracked through `PlayQueue.origin`: the session stamps
    the queue when it starts a station and only refills while that stamp
    survives. Playing an album, a playlist or a search result replaces
    the queue with a different origin, which ends the station without
    any extra teardown.
    """

    __gtype_name__ = "JamjarRadioSession"
    __gsignals__ = {
        "station-changed": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
    }

    def __init__(self, app) -> None:
        super().__init__()
        self.app = app
        self.station: Station | None = None
        self._served: list[str] = []
        self._loading = False
        if app.queue is not None:
            app.queue.connect("current-changed", self._on_current_changed)
        if app.player is not None:
            # Gapless and crossfade transitions advance the queue with
            # `emit_current_changed=False` — the player's track-changed is
            # the only signal that fires for every kind of transition, so
            # the refill has to hang off it too.
            app.player.connect("track-changed", self._on_track_changed)

    # ------- control -------

    def start(self, station: Station) -> None:
        """Fetch the station's first batch and hand it to the queue."""
        if self.app.client is None or self.app.queue is None:
            return
        self.station = station
        self._served = []
        self._fetch(station, first=True)
        self.emit("station-changed", station)

    def stop(self) -> None:
        if self.station is None:
            return
        self.station = None
        self._served = []
        self.emit("station-changed", None)

    @property
    def origin(self) -> str | None:
        return f"radio:{self.station.key}" if self.station else None

    # ------- internals -------

    def _remember(self, tracks) -> None:
        self._served.extend(t.id for t in tracks)
        if len(self._served) > SERVED_MEMORY:
            del self._served[:-SERVED_MEMORY]

    def _on_current_changed(self, queue, _track) -> None:
        if self.station is None:
            return
        if queue.origin != self.origin:
            # Something else took the queue over — the station is done.
            self.stop()
            return
        self._maybe_refill()

    def _on_track_changed(self, _player, _track) -> None:
        self._maybe_refill()

    def _maybe_refill(self) -> None:
        queue = self.app.queue
        if self.station is None or queue is None:
            return
        if queue.origin != self.origin:
            return
        if len(queue) - queue.index > REFILL_THRESHOLD:
            return
        self._fetch(self.station, first=False)

    def _fetch(self, station: Station, *, first: bool) -> None:
        if self._loading:
            return
        self._loading = True
        exclude = set(self._served)

        async def runme():
            client = self.app.client
            if station.kind == "mix":
                tracks = await client.instant_mix(station.seed_id, limit=BATCH_SIZE)
                return [t for t in tracks if t.id not in exclude]
            return await client.random_tracks(
                genres=list(station.genres) or None,
                years=list(station.years) or None,
                tags=list(station.tags) or None,
                favorites=station.favorites,
                limit=BATCH_SIZE,
                exclude_ids=exclude,
            )

        def done(future):
            self._loading = False
            try:
                tracks = future.result()
            except Exception as e:
                log.warning("radio fetch failed for %s: %s", station.key, e)
                if first:
                    GLib.idle_add(self._report, f"Couldn't start {station.title} radio.")
                return
            GLib.idle_add(self._apply, station, tracks, first)

        self.app.runner.submit(runme()).add_done_callback(done)

    def _report(self, message: str) -> bool:
        if self.station is not None:
            self.stop()
        if getattr(self.app, "show_toast", None):
            self.app.show_toast(message)
        return False

    def _apply(self, station: Station, tracks, first: bool) -> bool:
        if self.station is not station:
            return False  # user picked another station while this was in flight
        queue = self.app.queue
        if queue is None:
            return False
        if not tracks:
            if first:
                self._report(f"No tracks for {station.title} radio.")
            else:
                # A station that has run out of library isn't an error; it
                # just stops being endless.
                log.debug("radio %s exhausted", station.key)
                self.stop()
            return False

        self._remember(tracks)
        if first:
            # replace() emits current-changed, which the player turns into
            # playback — no explicit play() needed.
            queue.replace(tracks, start_index=0, origin=self.origin)
        else:
            queue.append(tracks)
        return False
