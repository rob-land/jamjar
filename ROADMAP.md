# Jamjar Roadmap

A snapshot of where the app sits relative to peer music players, what gaps are
worth closing, and the order to close them in. The concrete work items below
flow into [`TODO.md`](./TODO.md); this document is the *why*.

Date of evaluation: 2026-04-28. Tier 1 fully drained as of 2026-05-03; Tier 2 next. See [`TODO.md`](./TODO.md) for the live backlog.

---

## How Jamjar compares today

**On par with modern GNOME apps.** Adaptive libadwaita layout, sidebar
navigation, GTK4 navigation stack, blueprint UI, system MPRIS hookup, dark mode
honored. The foundation is stronger than Lollypop or Rhythmbox and visually
reads as a "real" GNOME app already.

**Closest peer is Symfonium (Android Jellyfin client).** Symfonium is the
most useful comparison: same backend, same audience, more mature. It's the
gap-to-close benchmark for feature work.

**Other reference points:**

| Player        | What it does well that Jamjar doesn't yet           |
|---------------|-----------------------------------------------------|
| Symfonium     | Context menus, smart queue ops, EQ, offline sync   |
| Amberol       | Tactile feel, smooth transitions, polish           |
| Spotify/Apple | Curated radio, lyrics view, polished now-playing    |
| Lollypop      | Party mode, last.fm integration                     |
| Rhythmbox     | Internet radio, podcast support (out of scope)      |

---

## Gaps, ranked by impact/effort

### Tier 1 — done

All six items shipped, plus the obvious follow-ups (cross-surface favorite
sync, search-row context menu, per-row heart indicators, click-to-seek on
synced lyric lines, jump-to-letter index on library tabs).

1. ✅ **Track context menus** — Play Now, Play Next, Add to Queue, Go to Album,
   Go to Artist, Toggle Favorite. Plus an album context menu with the same
   shape on every album tile, and lazy-fetch context menus on search rows.
2. ✅ **Favorite / heart button** on the bar, Now Playing page, and album/
   artist headers — all kept in sync via a `favorite-changed` signal on
   `JamjarApplication`. Per-row heart suffixes on queue / album-tracks /
   playlist-tracks rows update live.
3. ✅ **Lyrics view** on Now Playing with synced highlighting, centered
   auto-scroll, click-a-line-to-seek, and smooth opacity transitions.
4. ✅ **Volume slider** — vertical popover slider on the bar (desktop only),
   bound to player volume, persisted to GSettings, with the speaker icon
   updating per level.
5. ✅ **Sleep timer** — primary-menu entry with 15/30/45/60-min presets and a
   custom 1–480 min spinbutton; linear fade to 0 over 10 s on expiry, then
   pause + restore volume.
6. ✅ **Search button on page headers** — removed from sidebar destinations;
   each top-level page header has a magnifying-glass `Button` bound to
   `app.search`, positioned per GNOME convention.

### Tier 2 — moderate effort, common expectations

7. **Drag-to-reorder queue.** Queue is currently view-only beyond
   remove/jump.
8. **Add-to-playlist** dialog + **playlist editing** (rename, reorder, add
   tracks). DESIGN.md flags this for v0.2.
9. **Sort / filter on Library pages.** Sort albums by year/name/recently
   added; filter by genre. Jellyfin supports the query params; just not
   exposed.
10. **Up-next preview popover** on the bar showing the next 2–3 tracks. Cheap
    polish that mature players have.
11. **History page** (proper Recently Played view, not just the home shelf).

### Tier 3 — bigger lift, clear feature parity

12. **Instant Mix / radio from any item** — Jellyfin's
    `/Items/{id}/InstantMix` is the engine. Start with "Start radio from this
    album/artist" via context menu, then expand to genre/era/mood selection
    (existing TODO).
13. **Offline downloads** (DESIGN.md v0.3). Symfonium's killer feature for
    phone users.
14. **Smart playlists** / saved searches.

### Deliberately skipped

- **Equalizer.** ReplayGain via `rgvolume` already handles loudness; full EQ
  is rarely tweaked once set.
- **Cast / UPnP.** Heavy lift, narrow audience — leave at v0.4 per DESIGN.md.
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
- **JSON response cache + manual refresh** (TODO #16) — pairs naturally with
  any new feature that needs a "Refresh" affordance.
- **Empty space under Recently Added tiles** (TODO #13) — layout glitch.
