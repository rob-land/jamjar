"""The offline index — what makes downloaded music playable with no server.

The index is the only thing standing between "downloaded" and "gone":
it maps item ids to files and carries enough track metadata to render
the Downloaded page without asking the server anything. If the metadata
round-trip breaks, the page still lists rows — they just have no names,
which is the kind of failure that survives a quick look.
"""
import sqlite3

import gi

gi.require_version("Gst", "1.0")

from jamjar.models import Track  # noqa: E402
from jamjar.offline import OfflineIndex, track_from_meta, track_meta  # noqa: E402


def _track(tid="t1"):
    return Track(id=tid, name="Teardrop", album="Mezzanine", album_id="al1",
                 artists=("Massive Attack",), artist_ids=("ar1",),
                 duration_ticks=3300000000, album_image_tag="tag1")


def _index(tmp_path, name="index.sqlite"):
    return OfflineIndex(tmp_path / name)


def _file(tmp_path, name="a.audio", data=b"audio"):
    path = tmp_path / name
    path.write_bytes(data)
    return path


# --- basics ------------------------------------------------------------

def test_lookup_returns_the_stored_path(tmp_path):
    index = _index(tmp_path)
    index.insert("t1", str(_file(tmp_path)), 5)
    assert index.lookup("t1") == str(tmp_path / "a.audio")


def test_lookup_of_an_unknown_track_is_none(tmp_path):
    assert _index(tmp_path).lookup("nope") is None


def test_insert_twice_updates_rather_than_duplicates(tmp_path):
    index = _index(tmp_path)
    index.insert("t1", "/old", 5)
    index.insert("t1", "/new", 9)
    assert index.lookup("t1") == "/new"
    assert index.total_bytes() == 9


def test_total_bytes_of_an_empty_index_is_zero(tmp_path):
    assert _index(tmp_path).total_bytes() == 0


# --- metadata round-trip ----------------------------------------------

def test_metadata_survives_the_round_trip(tmp_path):
    index = _index(tmp_path)
    original = _track()
    index.insert(original.id, "/path", 10, meta=track_meta(original))
    restored = track_from_meta(index.entries()[0])
    assert restored.id == original.id
    assert restored.name == original.name
    assert restored.album == original.album
    assert restored.artists == original.artists
    assert restored.duration_seconds == original.duration_seconds


def test_entries_without_metadata_still_list(tmp_path):
    # Rows written before the metadata column existed.
    index = _index(tmp_path)
    index.insert("t1", "/path", 10)
    entry = index.entries()[0]
    assert entry["item_id"] == "t1"
    assert track_from_meta(entry).name == ""


def test_entries_are_newest_first(tmp_path):
    index = _index(tmp_path)
    index.insert("old", "/a", 1)
    index._conn.execute("UPDATE audio SET added_at = 1 WHERE item_id = 'old'")
    index.insert("new", "/b", 1)
    assert [e["item_id"] for e in index.entries()] == ["new", "old"]


# --- removal and eviction ---------------------------------------------

def test_remove_deletes_the_file_and_the_row(tmp_path):
    index = _index(tmp_path)
    path = _file(tmp_path)
    index.insert("t1", str(path), 5)
    index.remove("t1")
    assert index.lookup("t1") is None
    assert not path.exists()


def test_remove_tolerates_a_missing_file(tmp_path):
    index = _index(tmp_path)
    index.insert("t1", str(tmp_path / "gone.audio"), 5)
    index.remove("t1")
    assert index.lookup("t1") is None


def test_evict_lru_drops_least_recently_played_until_under_budget(tmp_path):
    index = _index(tmp_path)
    for name in ("a", "b", "c"):
        index.insert(name, str(_file(tmp_path, f"{name}.audio")), 100)
    index.touch("c")  # c played most recently, so it should survive
    removed = index.evict_lru(max_bytes=150)
    assert removed == 2
    assert index.lookup("c") is not None


def test_evict_lru_keeps_everything_under_budget(tmp_path):
    index = _index(tmp_path)
    index.insert("a", str(_file(tmp_path)), 10)
    assert index.evict_lru(max_bytes=1000) == 0
    assert index.lookup("a") is not None


# --- schema migration --------------------------------------------------

def test_opens_a_pre_metadata_database(tmp_path):
    # The column was added after the table shipped; opening an old index
    # must migrate rather than raise.
    path = tmp_path / "index.sqlite"
    conn = sqlite3.connect(str(path))
    conn.execute("""CREATE TABLE audio (
                      item_id TEXT PRIMARY KEY, path TEXT NOT NULL,
                      size INTEGER NOT NULL, added_at INTEGER NOT NULL,
                      last_played INTEGER NOT NULL DEFAULT 0)""")
    conn.execute("INSERT INTO audio VALUES ('t1', '/p', 5, 1, 0)")
    conn.commit()
    conn.close()

    index = OfflineIndex(path)
    index.insert("t2", "/q", 7, meta={"name": "Later"})
    assert {e["item_id"] for e in index.entries()} == {"t1", "t2"}
