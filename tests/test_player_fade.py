"""Crossfade math and the decision to crossfade at all.

Both live at module scope in `player.py` precisely so they can be tested
without a GStreamer pipeline: instantiating `Player` builds two real
playbin3 decks, which CI has no audio device for.

The two things worth pinning:

  * `equal_power` must not dip in the middle. A linear ramp does — two
    uncorrelated signals at gain 0.5 sum to noticeably less power than
    either at 1.0, and the transition audibly ducks.
  * `should_crossfade` decides when an overlap is wrong: repeat-one would
    flange a track against itself, and consecutive album tracks are
    sequenced deliberately, so they stay gapless unless asked otherwise.
"""
import math

import gi
import pytest

# The launcher is the single require_version declaration site at runtime;
# tests import jamjar modules directly, so declare Gst here before
# `jamjar.player` pulls it in (same preamble as test_view_imports.py).
gi.require_version("Gst", "1.0")

from jamjar.models import Track  # noqa: E402
from jamjar.player import equal_power, should_crossfade  # noqa: E402
from jamjar.queue import RepeatMode  # noqa: E402


def _t(label: str, album_id: str = "") -> Track:
    return Track(
        id=label, name=label, album="", album_id=album_id,
        artists=(), artist_ids=(), duration_ticks=0,
    )


# --- equal_power -------------------------------------------------------

def test_endpoints_are_full_and_silent():
    assert equal_power(0.0) == pytest.approx((1.0, 0.0))
    assert equal_power(1.0) == pytest.approx((0.0, 1.0), abs=1e-9)


def test_midpoint_holds_power_constant():
    out, inc = equal_power(0.5)
    assert out == pytest.approx(inc)
    assert out == pytest.approx(math.sqrt(0.5))


def test_power_sum_is_constant_across_the_fade():
    for step in range(21):
        out, inc = equal_power(step / 20)
        assert out ** 2 + inc ** 2 == pytest.approx(1.0)


def test_progress_is_clamped():
    assert equal_power(-2.0) == pytest.approx((1.0, 0.0))
    assert equal_power(5.0) == pytest.approx((0.0, 1.0), abs=1e-9)


# --- should_crossfade --------------------------------------------------

def _decide(current, nxt, seconds=6.0, repeat=RepeatMode.OFF, albums=False):
    return should_crossfade(current, nxt, seconds=seconds,
                            repeat=int(repeat), album_crossfade=albums)


def test_crossfades_between_unrelated_tracks():
    assert _decide(_t("a", "album-1"), _t("b", "album-2")) is True


def test_no_crossfade_when_disabled():
    assert _decide(_t("a"), _t("b"), seconds=0) is False


def test_no_crossfade_without_a_next_track():
    assert _decide(_t("a"), None) is False


def test_no_crossfade_on_repeat_one():
    assert _decide(_t("a"), _t("a"), repeat=RepeatMode.ONE) is False


def test_album_tracks_stay_gapless_by_default():
    assert _decide(_t("a", "album-1"), _t("b", "album-1")) is False


def test_album_tracks_crossfade_when_opted_in():
    assert _decide(_t("a", "album-1"), _t("b", "album-1"), albums=True) is True


def test_missing_album_ids_do_not_count_as_the_same_album():
    # Two tracks with no album id at all are unrelated, not "same album".
    assert _decide(_t("a"), _t("b")) is True


def test_no_current_track_still_crossfades():
    assert _decide(None, _t("b")) is True
