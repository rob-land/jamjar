"""Queue.move — drag-to-reorder math.

The drag-to-reorder feature (Tier 2 #7) hinges on `Queue.move`
keeping the play head pinned across the reorder. The cases:

  * src == index — current track being dragged: index moves to dst.
  * src < index <= dst — dragging a track from above the play head
    to below it: index shifts down by one.
  * dst <= index < src — dragging a track from below the play head
    to above it: index shifts up by one.
  * src and dst both on the same side of the play head: index
    unchanged.

Off-by-one in any of those silently puts "what's playing now" out
of sync with the speaker indicator, and the user notices because
"skip next" plays the wrong track. Regression cover for each.
"""
import pytest

from jamjar.models import Track
from jamjar.queue import PlayQueue


def _t(label: str) -> Track:
    """Cheap Track constructor — most fields irrelevant for queue math."""
    return Track(
        id=label, name=label, album="", album_id="",
        artists=(), artist_ids=(), duration_ticks=0,
    )


def _q(labels: list[str], index: int = 0) -> PlayQueue:
    q = PlayQueue(client=None)
    q.replace([_t(label) for label in labels], start_index=index)
    return q


# --- happy-path reorders -----------------------------------------------

def test_move_swaps_two_adjacent_tracks():
    q = _q(["a", "b", "c", "d"], index=0)
    q.move(1, 2)
    assert [t.id for t in q.tracks] == ["a", "c", "b", "d"]


def test_move_to_end():
    q = _q(["a", "b", "c"], index=0)
    q.move(0, 2)
    assert [t.id for t in q.tracks] == ["b", "c", "a"]


def test_move_to_start():
    q = _q(["a", "b", "c"], index=0)
    q.move(2, 0)
    assert [t.id for t in q.tracks] == ["c", "a", "b"]


# --- play-head adjustment ---------------------------------------------

def test_moving_the_current_track_keeps_it_current():
    """If the user reorders the track that's currently playing,
    the speaker indicator should follow it to the new slot — not
    stay on the old position pointing at a different song."""
    q = _q(["a", "b", "c"], index=0)  # playing "a"
    q.move(0, 2)
    assert q.tracks[q.index].id == "a"
    assert q.index == 2


def test_moving_above_head_to_below_decrements_index():
    """Dragging track #1 from above the head to slot #3 — the head
    (currently slot #2 = "c") shifts up to slot #1 because one
    track was removed from above it."""
    q = _q(["a", "b", "c", "d", "e"], index=2)  # playing "c"
    q.move(0, 3)  # move "a" to slot 3
    assert [t.id for t in q.tracks] == ["b", "c", "d", "a", "e"]
    assert q.tracks[q.index].id == "c"
    assert q.index == 1


def test_moving_below_head_to_above_increments_index():
    """Dragging track #3 (below the head) to slot #0 — the head
    (currently slot #2) shifts down to slot #3 because a track
    was inserted above it."""
    q = _q(["a", "b", "c", "d", "e"], index=2)  # playing "c"
    q.move(4, 0)  # move "e" to slot 0
    assert [t.id for t in q.tracks] == ["e", "a", "b", "c", "d"]
    assert q.tracks[q.index].id == "c"
    assert q.index == 3


def test_moving_between_two_tracks_both_above_head_leaves_index_alone():
    """Reorders that stay on one side of the play head don't affect it."""
    q = _q(["a", "b", "c", "d", "e"], index=4)  # playing "e"
    q.move(0, 1)  # swap "a" and "b"
    assert [t.id for t in q.tracks] == ["b", "a", "c", "d", "e"]
    assert q.tracks[q.index].id == "e"
    assert q.index == 4


def test_moving_between_two_tracks_both_below_head_leaves_index_alone():
    q = _q(["a", "b", "c", "d", "e"], index=0)  # playing "a"
    q.move(3, 4)  # swap "d" and "e"
    assert [t.id for t in q.tracks] == ["a", "b", "c", "e", "d"]
    assert q.tracks[q.index].id == "a"
    assert q.index == 0


# --- guard rails -------------------------------------------------------

def test_move_with_out_of_range_src_is_noop():
    q = _q(["a", "b"], index=0)
    q.move(99, 0)
    assert [t.id for t in q.tracks] == ["a", "b"]
    assert q.index == 0


def test_move_with_out_of_range_dst_is_noop():
    q = _q(["a", "b"], index=0)
    q.move(0, 99)
    assert [t.id for t in q.tracks] == ["a", "b"]
    assert q.index == 0


def test_move_to_same_position_is_noop():
    """Dropping a row onto itself — guarded at the view layer but
    the model should also handle it without scrambling indices."""
    q = _q(["a", "b", "c"], index=1)
    q.move(1, 1)
    assert [t.id for t in q.tracks] == ["a", "b", "c"]
    assert q.index == 1


# --- restore ------------------------------------------------------------
#
# Restoring a saved queue must not look like "the user pressed play":
# the player turns `current-changed` into playback, so an app that
# emitted it at startup would begin blasting music on launch.

def test_restore_does_not_emit_current_changed():
    q = PlayQueue(client=None)
    fired = []
    q.connect("current-changed", lambda _q, t: fired.append(t))
    q.restore([_t("a"), _t("b")], 1)
    assert fired == []


def test_restore_sets_the_play_head():
    q = PlayQueue(client=None)
    q.restore([_t("a"), _t("b"), _t("c")], 2)
    assert q.index == 2
    assert q.current.id == "c"


def test_restore_announces_the_new_queue():
    q = PlayQueue(client=None)
    changed = []
    q.connect("queue-changed", lambda _q: changed.append(True))
    q.restore([_t("a")], 0)
    assert changed == [True]


def test_restore_clamps_an_out_of_range_index():
    q = PlayQueue(client=None)
    q.restore([_t("a"), _t("b")], 99)
    assert q.index == 0


def test_restore_of_an_empty_queue_leaves_no_current():
    q = PlayQueue(client=None)
    q.restore([], 3)
    assert q.index == -1
    assert q.current is None


def test_restore_clears_a_stale_radio_origin():
    q = PlayQueue(client=None)
    q.replace([_t("a")], origin="radio:decade:1980")
    q.restore([_t("b")], 0)
    assert q.origin is None
