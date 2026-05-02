# Jamjar TODO

Ordering follows [`ROADMAP.md`](./ROADMAP.md): Tier 1 (small UX wins) first,
then Tier 2 (common expectations), then standing items (existing TODOs).

---

## Tier 1 — small effort, big UX win

### 1. Track + album context menus — follow-ups
`views/track_menu.py` provides `install_track_menu(widget, get_track, app,
window)` (right-click + long-press), used by album, queue, playlist, and
library Songs rows. Menu items: Play Now, Play Next, Add to Queue, Go to
Album, Go to Artist, Toggle Favorite.

`views/album_menu.py` provides `install_album_menu(...)` for album tiles
in the library Albums grid, the artist page's albums grid, and the home
Recently Added row. Menu items: Play Now, Play Next, Add to Queue, Go to
Artist, Toggle Favorite. Track-list-needing actions go through
`Library.album_tracks` (cached after first fetch).

Still to do:
- **Search results.** Search rows only carry a `SearchHit` (`item_id`), not
  a full Track. Either fetch the Track on right-click before showing the
  menu, or pre-fetch when binding the row.
- **Add to Playlist** menu item — depends on Tier 2 #8.
- **Visual feedback after Toggle Favorite.** Mutating `track.user_data` /
  `album.user_data` only updates the next menu open; the row/tile UI
  itself doesn't reflect the new state. Wire a small heart indicator on
  each row/tile that updates immediately.

### 2. Favorite / heart button — done
Implemented a `ToggleButton` with `emblem-favorite-symbolic` on both the
bottom bar (hidden in compact mode) and the Now Playing page header. State
syncs from `track.user_data["IsFavorite"]` on track-changed, and toggle
optimistically updates the visual + mutates `user_data` on success;
reverts on REST failure. Shared helpers `_apply_favorite_visual` and
`_commit_favorite` in `views/now_playing.py`.

Follow-ups (rolled in with #1's deferred list):
- Cross-surface sync when the favorite state changes in one place while
  others are visible (e.g., toggle from a row's context menu while the
  same track is in the bar).

### 3. Lyrics view — done
Cover on the Now Playing page is wrapped in an `Adw.Carousel` with two
pages — Cover and Lyrics — plus indicator dots beneath. Lyrics fetched via
`fetch_lyrics` on track-changed (placeholder while loading;
"No lyrics available" if 404/empty). Synced lyrics highlight the active
line via `lyrics-line.active` based on `position-changed`, using the
existing `active_index` helper.

Follow-ups:
- Auto-scroll the lyrics view to keep the active line visible.
- Queue pane as a third carousel page (originally proposed in DESIGN.md).

### 4. Volume slider in the bar (desktop) — done
`MenuButton volume_button` between repeat and expand on the now-playing bar.
Popover holds a vertical `Gtk.Scale` (0..1, 140 px tall) bound to the
player's volume; speaker icon updates per level (muted/low/medium/high) as
the slider moves. Hidden in compact mode alongside the other long-form
widgets. Persists to GSettings `volume` (already restored at attach via
`Player.configure`); slider re-syncs from `player.volume` each time the
popover opens so future MPRIS/external volume changes show up.

### 5. Sleep timer — done
`SleepTimer` (`jamjar/sleep_timer.py`) owns countdown + fade-out; presents
via `SleepTimerDialog` (`views/sleep_timer.py`) launched from
`app.sleep-timer` (primary-menu "Sleep Timer…"). Dialog offers
15/30/45/60-min presets and a custom SpinButton (1..480 min); persists last
custom value to GSettings `sleep-timer-default-minutes`. When a timer is
active the dialog flips to a confirmation view ("Stopping in N minutes")
with a destructive "Cancel Timer" response. On expiry the volume tapers
linearly to 0 over 10 s in 50 ms steps, `Player.pause()` is called, and
the original volume is restored so the next play session isn't silent.
Mid-fade cancel restores the original volume immediately.

### 6. Search icon in sidebar header
Move search from a sidebar destination to a magnifying-glass button in the
sidebar header (upper-left), matching the standard GNOME pattern (Files,
Photos, etc.). Toggling it slides down a search bar and focuses it. Keeps
`Ctrl+F` working.

---

## Tier 2 — moderate effort, common expectations

### 7. Drag-to-reorder queue
Currently the queue page only supports remove and jump. Add drag handles or
press-and-hold drag on the rows so users can reorder upcoming tracks.

### 8. Add to playlist + playlist editing
Wire "Add to playlist" (from the new context menu) to a dialog that lists the
user's playlists and offers "Create new". On the Playlist page, support
rename, delete, and reorder of tracks (drag, like the queue). Backed by the
Jellyfin Playlists endpoints.

### 9. Sort / filter on Library pages
Albums and Songs need sort options (name, year, recently added, artist) and
filter options (genre at minimum). Jellyfin supports the query params via
`SortBy` / `Genres` / `Years`; exposing them is mostly UI.

### 10. Up-next preview on the bar
Small popover from the bar (or a hover/long-press on the bar) showing the
next 2–3 tracks with covers. Cheap polish that signals "this app knows what
it's doing."

### 11. History page
A proper "Recently Played" list (separate from the home shelf), backed by
`/Users/{u}/Items/Resume` or the play history endpoint. Reachable from the
sidebar (or a sub-nav under Home).

---

## Standing items

### 12. Genre / era / mood radio channels
Investigate whether Jellyfin supports (or can be made to support) "radio"-
style endless channels filtered by:
- Genre
- Era / decade
- Mood

Check Jellyfin's `/Items` query options (Genres, Years, instant mix endpoints
like `/Items/{id}/InstantMix`) and design a UI for selecting and starting
these stations. Consider seeding this off the Tier 1 context menu first
("Start radio from this album/artist") before tackling the standalone
station-picker UI.

### 13. Empty space under Recently Added tiles
After cover images load, an empty band appears between the bottom of each
Recently Added tile and the next section heading. Activation works; only the
row's vertical sizing is off. Tried scaling the loaded pixbuf to the
picture's request size and setting `valign=START` on the tile button;
neither fully eliminates the gap. Likely needs the row's height pinned via
`height-request` on the row Box, or a measure-overlay-style constraint on
the tile.

### 14. Recently Played and Suggested are empty — done
`client.recently_played_tracks()` queries `/Items` sorted by `DatePlayed`
desc with `Filters=IsPlayed` (Jellyfin has no dedicated endpoint;
`/Users/{u}/Items/Resume` is for paused playback, not history).
`client.suggestions()` calls `/Users/{userId}/Suggestions?mediaType=Audio`
and filters defensively to `Type=Audio`. Both feed dedicated `Library`
list stores (`recently_played`, `suggested`) wired into the home page via
a new `_tile_for_track` builder — clicking a track tile replaces the
queue with just that track and starts playback; right-click opens the
existing track menu (Play Now / Next / Add to Queue / Go to Album / Go
to Artist / Toggle Favorite). Album/track tile repaints share a
`_repaint_row(row, store, tile_builder)` helper.

### 15. Clickable artist and album names
Wherever an artist name or album name is displayed (track rows, now-playing
surfaces, album/playlist headers), make those names act as links that
navigate to the corresponding artist/album page.

### 16. Persistent response cache for library JSON + manual refresh
On a large collection, every cold start re-fetches Albums / Artists / Songs /
Playlists from scratch. Wire up a SQLite cache (e.g.
`aiohttp-client-cache`) under `GLib.get_user_cache_dir() / "jamjar" / "http"`
so the warm path is instant, then revalidate in the background. Use a sane
TTL or ETag/Last-Modified if Jellyfin supplies them. Pair this with a
manual refresh action (pull-to-refresh on phone, header refresh button on
desktop) so newly-added or removed items can be picked up without waiting
for TTL expiry. The image cache (`jamjar/imagecache.py`, done) is keyed by
`imageTag` and self-invalidates; the JSON cache will need explicit
invalidation, which is why this is its own item.
