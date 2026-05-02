# Jamjar Roadmap

A snapshot of where the app sits relative to peer music players, what gaps are
worth closing, and the order to close them in. The concrete work items below
flow into [`TODO.md`](./TODO.md); this document is the *why*.

Date of evaluation: 2026-04-28.

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

### Tier 1 — small effort, big UX win

These are each roughly one focused session and together transform the app from
"library viewer" to "real music client."

1. **Track context menus** — right-click any track row: Play next, Add to
   queue, Go to album, Go to artist, Toggle favorite, Add to playlist.
   Symfonium and every desktop player has this; the absence makes Jamjar feel
   passive.
2. **Favorite / heart button** on the bar and Now Playing surfaces.
   Endpoint already exists (`POST/DELETE /Users/{u}/FavoriteItems/{id}`),
   just no UI.
3. **Lyrics view** on Now Playing. Endpoint exists (`/Audio/{id}/Lyrics`),
   DESIGN.md mentions it, the phone Now Playing already has a Carousel slot
   for it.
4. **Volume slider** in the bar (desktop). Phones use hardware keys; desktops
   expect a slider.
5. **Sleep timer** in the primary menu.
6. **Search icon in sidebar header** — move search from a menu item to a
   magnifying-glass button in the sidebar's upper-left, matching GNOME's
   standard pattern (Files, Photos, etc.).

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

Do Tier 1 in order before chasing the larger items in TODO (radio channels,
persistent image cache). Each Tier 1 item closes a gap users would notice
within minutes of opening the app. Tier 3 should come after the Tier 2
foundations they depend on (e.g. context-menu plumbing makes "Start radio from
this album" trivial).
