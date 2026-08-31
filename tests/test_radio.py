"""Station building — the pure half of the radio feature.

`build_stations` turns a library's filter vocabulary (`/Items/Filters`
genres + years, plus tags) into the cards on the Radio page. It has to
survive libraries that are barely tagged at all, which is the common
case: the failure mode worth guarding is offering a station that returns
nothing, so a mood whose genres the library doesn't have must not
appear.
"""
import pytest

from jamjar.radio import (
    MOOD_GENRES,
    REFILL_THRESHOLD,
    Station,
    build_stations,
    mix_station,
)


def _by_kind(stations, kind):
    return [s for s in stations if s.kind == kind]


def _titles(stations, kind):
    return [s.title for s in _by_kind(stations, kind)]


# --- always-available stations ----------------------------------------

def test_empty_library_still_offers_favorites_and_surprise():
    stations = build_stations([], [], [])
    assert _titles(stations, "favorites") == ["Favorites"]
    assert _titles(stations, "surprise") == ["Surprise Me"]
    assert _by_kind(stations, "mood") == []
    assert _by_kind(stations, "decade") == []


def test_favorites_station_carries_the_favorites_flag():
    favorites = _by_kind(build_stations([], [], []), "favorites")[0]
    assert favorites.favorites is True
    assert favorites.genres == ()


# --- moods -------------------------------------------------------------

def test_mood_appears_only_when_the_library_has_its_genres():
    stations = build_stations(["Ambient", "Polka"], [], [])
    assert "Chill" in _titles(stations, "mood")
    assert "Dancefloor" not in _titles(stations, "mood")


def test_mood_keeps_only_the_genres_that_exist():
    chill = next(s for s in build_stations(["Ambient", "Jazz"], [], [])
                 if s.title == "Chill")
    assert set(chill.genres) == {"Ambient", "Jazz"}


def test_mood_genre_matching_ignores_case_and_keeps_library_spelling():
    # Jellyfin returns whatever the tags say; the query has to send that
    # spelling back, not the one in MOOD_GENRES.
    chill = next(s for s in build_stations(["ambient"], [], [])
                 if s.title == "Chill")
    assert chill.genres == ("ambient",)


def test_every_mood_is_reachable_from_its_own_genre_list():
    for mood, genres in MOOD_GENRES.items():
        stations = build_stations([genres[0]], [], [])
        assert mood in _titles(stations, "mood")


# --- decades -----------------------------------------------------------

def test_decades_are_derived_and_newest_first():
    stations = build_stations([], [1983, 1987, 1994, 2001], [])
    assert _titles(stations, "decade") == ["2000s", "1990s", "1980s"]


def test_decade_station_covers_all_ten_years():
    eighties = next(s for s in build_stations([], [1983], [])
                    if s.title == "1980s")
    assert eighties.years == tuple(range(1980, 1990))


def test_bogus_years_are_dropped():
    # Jellyfin hands back 0 or 1 for items with a broken date tag.
    stations = build_stations([], [0, 1, 1975], [])
    assert _titles(stations, "decade") == ["1970s"]


# --- styles and tags ---------------------------------------------------

def test_every_genre_becomes_a_style_station():
    stations = build_stations(["Rock", "Dub"], [], [])
    assert _titles(stations, "style") == ["Rock", "Dub"]


def test_tags_become_stations():
    tag = next(s for s in build_stations([], [], ["roadtrip"], )
               if s.kind == "tag")
    assert tag.title == "roadtrip"
    assert tag.tags == ("roadtrip",)


def test_a_tag_that_duplicates_a_genre_or_mood_is_dropped():
    # Two cards labelled "Jazz" running different queries is just
    # confusing — the genre station already covers it.
    stations = build_stations(["Jazz"], [], ["jazz", "Chill", "gym"])
    assert _titles(stations, "tag") == ["gym"]


def test_station_keys_are_unique():
    stations = build_stations(["Rock", "Ambient"], [1991], ["gym"])
    keys = [s.key for s in stations]
    assert len(keys) == len(set(keys))


# --- instant mix seeds -------------------------------------------------

def test_mix_station_wraps_the_seed_item():
    class Item:
        id = "abc123"
        name = "Blue Lines"

    station = mix_station(Item(), kind_label="album")
    assert station.kind == "mix"
    assert station.seed_id == "abc123"
    assert station.title == "Blue Lines"
    assert "album" in station.subtitle


def test_stations_are_immutable():
    station = Station(key="k", title="t", kind="style")
    with pytest.raises(Exception):
        station.title = "other"


def test_refill_threshold_leaves_room_to_fetch():
    # The refill fires with tracks still queued so the fetch has time to
    # land before the queue runs dry.
    assert REFILL_THRESHOLD >= 3
