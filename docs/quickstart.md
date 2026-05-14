# Jamjar — Quick start

A full-featured Jellyfin music client for GNOME desktop and Phosh
phones (FuriLabs FLX1s, Librem 5, PineNote). GTK4 / libadwaita,
gapless GStreamer playback, MPRIS2 system integration, native
search + lyrics + scrobbling.

## Install

```bash
flatpak install --user rob-land land.rob.jamjar
```

## First-time setup

Launch Jamjar. The login dialog walks you through:

1. **Server discovery** — Jamjar broadcasts on UDP port 7359
   looking for Jellyfin servers; discovered servers appear as
   action rows you can tap. If your server doesn't show up
   (firewall, different network segment), tap **Connect
   manually** and paste the server URL.
2. **Sign in** — **Quick Connect** is the default (the server
   shows you a 6-digit code; confirm it from any signed-in
   Jellyfin client, like the web UI). Tap **Use password
   instead** for username + password.

After auth the library page loads. The first page of albums
fetches in the background; rails populate as data arrives.

## Daily use

### Layout

`Adw.OverlaySplitView` with a sidebar collapse breakpoint at
`max-width: 600sp`:

- **Sidebar** — navigation: Home, Library (Albums / Artists /
  Songs / Playlists), Search, Now Playing.
- **Content** — the view for the selected destination.
- **Now Playing bar** — pinned at the bottom of the window;
  shows current track + scrubber + transport. Tap it to expand
  into a full Now Playing page.

On phone widths the sidebar collapses; you drive between views
via the top-bar navigation. The Now Playing bar is the same.

### Library views

- **Albums** — grid view on desktop, list-of-Adw.ActionRows on
  phone. Cover art loads lazily; tap to open the album page.
- **Artists** — album-artists only (no track artists). Tapping
  an artist shows their albums.
- **Songs** — paginated `GtkColumnView` on desktop /
  `GtkListView` on phone. Search and jump-to-letter on the
  right.
- **Playlists** — your Jellyfin playlists.

### Jump-to-letter

Library views have an A-Z scrubber on the right edge — tap a
letter to jump to that section without scrolling. Works on both
phone and desktop layouts.

### Search

Top-bar search box on every library view. Matches across track
name + album + artist server-side via Jellyfin's
`/Search/Hints` endpoint. Results group by type (Songs / Albums
/ Artists).

### Now Playing

Compact bar at the bottom is always visible while a track is
loaded. Tap it for the full page:

- Cover art (full-bleed on phone, side-by-side with track info
  on desktop).
- Scrubber + transport (play/pause, skip, prev/next).
- **Queue** — current queue with reorder + remove.
- **Lyrics** — synced lyrics with auto-scroll if your tracks
  have `Lyrics.lrc` files synced to Jellyfin. Plain text
  lyrics work too (no scroll).

Phone Now Playing is an `Adw.Carousel` between Cover / Lyrics /
Queue — swipe between them.

### Playback details

- **Gapless** — playbin3 with `about-to-finish` for seamless
  next-track loads. Albums play through without gaps.
- **Codec policy** — Wi-Fi defaults to lossless / `audioCodec=
  copy`; mobile data defaults to 128k Opus. Per-network split
  configurable in Preferences.
- **ReplayGain** — `rgvolume` element with a per-track / per-album
  toggle in Preferences (off by default).
- **Sleep timer** — fade-to-silence over 10 seconds, then pause.
  Top-bar Now Playing menu → Sleep Timer.

### MPRIS integration

Jamjar registers as `org.mpris.MediaPlayer2.land.rob.jamjar`.
Media keys work, GNOME Shell quick settings shows the current
track, Phosh lockscreen displays cover + transport controls.

## Favorites and queue

- **Favorite** a track from the row context menu or the Now
  Playing detail. Favorites sync to Jellyfin
  (`/Users/{u}/FavoriteItems/{id}`).
- **Play next** queues a track right after the current one.
- **Play later** appends to the queue.
- **Shuffle / repeat** in the Now Playing transport (cycle: off
  → all → one).

## Scrobbling

Jellyfin's internal scrobbling (`/Sessions/Playing` +
`/Sessions/Playing/Progress` + `/Sessions/Playing/Stopped`)
runs automatically — your Jellyfin "Recently Played" stays in
sync.

External scrobblers (Last.fm, ListenBrainz) aren't wired up in
Jamjar directly. The Jellyfin Trakt plugin scrobbles
*through* Jellyfin, which is the path most people use.

## Where things are kept

| What | Path |
| --- | --- |
| Server URL, device ID, prefs | GSettings (`land.rob.jamjar`) |
| Auth token (Jellyfin) | libsecret |
| On-disk cover cache | `~/.var/app/land.rob.jamjar/cache/jamjar/covers/` |
| Image cache (LRU, 200MB) | `~/.var/app/land.rob.jamjar/cache/jamjar/images/` |
| Logs | `~/.var/app/land.rob.jamjar/data/jamjar/jamjar.log` |

## Notable limits

- **Offline downloads** are roadmap, not built. The image cache
  is on disk; actual track downloads aren't.
- **No equalizer** — relies on GStreamer's default audio path.
  ReplayGain handles per-track volume; EQ comes later.
- **No drag-to-reorder queue** yet (Play next / Play later
  works; in-queue reordering is roadmap).
- **No track scrubbing on the Now Playing bar** — use the full
  Now Playing page or the scrubber expansion.
- **No GNOME Shell search provider** — typing in the overview
  doesn't find Jamjar tracks. Roadmap.
