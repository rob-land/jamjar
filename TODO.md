# Jamjar TODO

The live backlog. Tier 1 (small UX wins) is drained; Tier 2 (common
expectations) is next, then Tier 3 (bigger lifts), then standing items.

**HIG audit follow-ups still open:** none — library-tab empty states and generic error toasts both wired (see `_observe_windowed` / `_refresh_playlists_stack` in `views/library.py` and `Application.show_toast` in `application.py`).

---

## Where Jamjar sits today

**On par with modern GNOME apps.** Adaptive libadwaita layout, sidebar
navigation, GTK4 navigation stack, blueprint UI, system MPRIS hookup, dark mode
honored. The foundation is stronger than Lollypop or Rhythmbox and visually
reads as a "real" GNOME app already.

**Closest peer is Symfonium (Android Jellyfin client).** Symfonium is the
most useful comparison: same backend, same audience, more mature. It's the
gap-to-close benchmark for feature work.

| Player        | What it does well that Jamjar doesn't yet           |
|---------------|-----------------------------------------------------|
| Symfonium     | Context menus, smart queue ops, EQ, offline sync   |
| Amberol       | Tactile feel, smooth transitions, polish           |
| Spotify/Apple | Curated radio, lyrics view, polished now-playing    |
| Lollypop      | Party mode, last.fm integration                     |
| Rhythmbox     | Internet radio, podcast support (out of scope)      |

---

## Tier 1 — small effort, big UX win (done)

### 1. Track + album context menus — done
`views/track_menu.py` provides `install_track_menu(...)` (right-click +
long-press) and `show_track_popover(track, app, window, parent, x, y)`
for callers without a Track on-hand at gesture time. Used by album,
queue, playlist, library Songs, and (via lazy fetch + caching) search
result rows. Menu items: Play Now, Play Next, Add to Queue, Go to
Album, Go to Artist, Toggle Favorite.

`views/album_menu.py` provides `install_album_menu(...)` for album
tiles in the library Albums grid, the artist page's albums grid, and
the home Recently Added row. Menu items: Play Now, Play Next, Add to
Queue, Go to Artist, Toggle Favorite. Track-list-needing actions go
through `Library.album_tracks` (cached after first fetch).

Cross-surface favorite sync via a new `favorite-changed (item_id,
is_favorite)` signal on `JamjarApplication`, emitted from
`commit_favorite` after the REST call succeeds. Subscribers: now-playing
bar + page favorite buttons, album page header, queue page rows
(`favorite_heart` suffix), album page track rows, playlist page rows
(via `store.items_changed` to nudge the factory rebind). New
`favorite_heart(is_favorite)` helper in `views/_common.py` keeps the
Image visible only when favorited.

Still deferred:
- **Add to Playlist** menu item — depends on Tier 2 #8.

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
existing `active_index` helper. Active line auto-scrolls into the
viewport (centered) on each transition; synced lines are clickable to
seek to that timestamp (`.lyrics-line-clickable` cursor + hover);
opacity transition smooths the active-line swap; scroll resets to top
on track change.

Follow-ups:
- Queue pane as a third carousel page — done. The Now Playing carousel now
  includes an Up Next page with current-track highlighting and tap-to-jump
  queue navigation.

### 4. Volume slider in the bar (desktop) — done
`MenuButton volume_button` between repeat and expand on the now-playing bar.
Popover holds a vertical `Gtk.Scale` (0..1, 140 px tall) bound to the
player's volume; speaker icon updates per level (muted/low/medium/high) as
the slider moves. Hidden in compact mode alongside the other long-form
widgets. Persists to GSettings `volume` (already restored at attach via
`Player.configure`); slider re-syncs from `player.volume` each time the
popover opens so future MPRIS/external volume changes show up.

### 5. Sleep timer — done
`SleepTimer` (`src/jamjar/sleep_timer.py`) owns countdown + fade-out; presents
via `SleepTimerDialog` (`views/sleep_timer.py`) launched from
`app.sleep-timer` (primary-menu "Sleep Timer…"). Dialog offers
15/30/45/60-min presets and a custom SpinButton (1..480 min); persists last
custom value to GSettings `sleep-timer-default-minutes`. When a timer is
active the dialog flips to a confirmation view ("Stopping in N minutes")
with a destructive "Cancel Timer" response. On expiry the volume tapers
linearly to 0 over 10 s in 50 ms steps, `Player.pause()` is called, and
the original volume is restored so the next play session isn't silent.
Mid-fade cancel restores the original volume immediately.

### 6. Search button in page headers — done
Removed `("search", ...)` from `SIDEBAR_PAGES`; SearchPage stays as a
NavigationView destination but no longer occupies a sidebar slot. Each
top-level page header now carries a magnifying-glass `Button` bound to
`app.search`, positioned per GNOME convention: rightmost on pages
without a page menu (Home, Now Playing, Library — Library was already
done), and to the left of the page menu where one exists (Queue).
`Ctrl+F` still works (unchanged accelerator on `app.search`). The
"slide-down search bar" pattern from the original spec was not added —
Jamjar's search is global rather than view-filtering, so a separate
SearchPage destination matches the actual semantics better than a
contextual filter bar; the header button is the on-brand GNOME hook
either way.

---

## Tier 2 — moderate effort, common expectations

### 7. Drag-to-reorder queue — done
Each queue row has a `list-drag-handle-symbolic` grip at its leading
edge. `Gtk.DragSource` on the handle emits the source queue index as a
GValue(int); a `Gtk.DropTarget` on the destination row reads it and
calls `Queue.move(src, target)` — which handles play-head adjustment
so the currently-playing track stays "current" across a reorder.
Dropping a row onto itself is a no-op so the user doesn't have to
aim outside the row to cancel. The row's tap-to-jump gesture is
preserved because the drag source is gated on the handle icon, not
the ActionRow body.

### 8. Add to playlist + playlist editing — done
Track context menu **Add to Playlist** (pick existing or create new). Playlist
page supports rename, delete, per-track remove, and drag-to-reorder (Jellyfin
`POST …/Move/{index}`, `DELETE …/Items?entryIds=…`).

### 9. Sort / filter on Library pages — partial
Albums and Songs have a header sort popover (title, year, recently added /
played, artist, shuffle). Letter jump still handles A–Z filtering. Genre /
year filters remain open.

### 10. Up-next preview on the bar — done
Small popover from the bar showing the next 2–3 tracks. Cheap polish that
signals "this app knows what it's doing."

Implemented as a desktop/wide-bar `MenuButton` showing the next three queue
items, wrapping when repeat-all is active. The compact phone bar stays lean;
phone queue preview lives on the Now Playing carousel's Up Next page.

### 11. History page — done
Sidebar **History** page lists up to 200 recently played tracks (same
`/Items?SortBy=DatePlayed&Filters=IsPlayed` query as the home shelf), with
track context menus and tap-to-play.

---

## Tier 3 — bigger lift, clear feature parity

### 12. Instant Mix / radio from any item — partial
Jellyfin's `/Items/{id}/InstantMix` is the engine. Start with "Start radio
from this album/artist" via context menu (Tier-2-adjacent quick win since the
context-menu plumbing already exists), then expand to a standalone station
picker filtered by genre / era / mood. Investigate which `/Items` query
params (`SortBy`, `Genres`, `Years`, instant-mix endpoints) the UI needs to
expose.

Track and album context menus now expose Start Track Radio / Start Album Radio,
and the artist detail page has Start Radio. All are backed by
`/Items/{id}/InstantMix`, replacing the queue and starting playback. Still open:
standalone station picker, genre / era / mood radio.

### 13. Offline downloads
Per `DESIGN.md` v0.3. Symfonium's killer feature for phone users.

### 14. Smart playlists / saved searches

---

## Standing items

### 15. Empty space under Recently Added tiles
After cover images load, an empty band appears between the bottom of each
Recently Added tile and the next section heading. Activation works; only the
row's vertical sizing is off. Tried scaling the loaded pixbuf to the
picture's request size and setting `valign=START` on the tile button;
neither fully eliminates the gap. Likely needs the row's height pinned via
`height-request` on the row Box, or a measure-overlay-style constraint on
the tile.

### 16. Recently Played and Suggested are empty — done
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

### 17. Clickable artist and album names — done (standalone labels)
Wired link affordance (`make_link_label`, `open_artist_by_id`,
`open_album_by_id` in `views/_common.py`; CSS `.link-label:hover {
text-decoration: underline }`) on the four standalone artist/album
labels: now-playing bar artist, now-playing page artist + album, and
album-page header artist. `track_menu.py` and `album_menu.py` were
refactored to use the same `open_*_by_id` helpers. Click target updates
on each track-changed; clearing target removes the affordance.

In-list rows (queue / playlist / album / songs ColumnView) deliberately
left non-clickable — they're activate-to-play surfaces and competing
inner targets would be confusing. Right-click menu on those rows still
offers "Go to Artist" / "Go to Album" for navigation.

### 18. Persistent response cache for library JSON + manual refresh
On a large collection, every cold start re-fetches Albums / Artists / Songs /
Playlists from scratch. Wire up a SQLite cache (e.g.
`aiohttp-client-cache`) under `GLib.get_user_cache_dir() / "jamjar" / "http"`
so the warm path is instant, then revalidate in the background. Use a sane
TTL or ETag/Last-Modified if Jellyfin supplies them. Pair this with a
manual refresh action (pull-to-refresh on phone, header refresh button on
desktop) so newly-added or removed items can be picked up without waiting
for TTL expiry. The image cache (`src/jamjar/imagecache.py`, done) is keyed by
`imageTag` and self-invalidates; the JSON cache will need explicit
invalidation, which is why this is its own item.

---

## Deliberately skipped

- **Equalizer.** ReplayGain via `rgvolume` already handles loudness; full EQ
  is rarely tweaked once set.
- **Cast / UPnP.** Heavy lift, narrow audience — leave at v0.4 per
  `DESIGN.md`.
- **Social features** (sharing, follow, comments). Not appropriate for a
  self-host client.
- **Personalised recommendations.** Jellyfin's `/Items/Suggestions` is
  enough; no need for our own ML.

---

## Recommended order

Tier 1 is drained. Tier 2 next, ideally in roughly this order: drag-to-
reorder queue (#7), playlist editing (#8 — also unblocks the deferred
"Add to Playlist" item from #1), sort/filter on Library pages (#9), then
the polish items #10–#11. Tier 3 should come after the Tier 2 foundations
they depend on (context-menu plumbing already makes "Start radio from this
album" trivial — that's a Tier-2-adjacent quick win).

Two perf/HIG items worth picking up alongside Tier 2 work:
- **JSON response cache + manual refresh** (#18) — pairs naturally with
  any new feature that needs a "Refresh" affordance.
- **Empty space under Recently Added tiles** (#15) — layout glitch.
