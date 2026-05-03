# Jamjar

A full-featured Jellyfin music client for **GNOME desktop** and **Phosh** (Linux phones — particularly the FuriLabs FLX1s). Built with GTK4 / libadwaita / Python, packaged as a Flatpak, with adaptive layouts that work in both portrait phone and docked desktop (NexDock XL) modes.

App ID: `land.rob.Jamjar`

This file is the orientation document for Claude Code working on this repo. The full architecture is in [`jamjar-design.md`](./jamjar-design.md) — read that first for any non-trivial change. The current backlog is [`TODO.md`](./TODO.md); the prioritisation rationale is [`ROADMAP.md`](./ROADMAP.md).

---

## What this app is

- **Backend:** Jellyfin (self-hosted media server). No other backends planned for v1.
- **Auth:** Server discovery via UDP broadcast on port 7359, with mDNS fallback. Both Quick Connect (preferred on phone) and username/password are supported. Tokens stored in libsecret, never on disk in plain text.
- **Audience:** Self-hosters who already run Jellyfin and want a real native Linux music app — not the Jellyfin web UI in a window, not a media-everything client like Jellyfin Media Player. Music-focused, polished, adaptive.
- **Why this exists:** The Linux music client landscape has lots of local-library players (Amberol, Lollypop, Rhythmbox) but no good GTK4/Phosh-adaptive Jellyfin music client. Symfonium fills this niche on Android; nothing comparable exists for GNOME/Phosh.

---

## Stack

| Layer | Choice |
|---|---|
| UI | GTK 4.14+ / libadwaita 1.5+ |
| Language | Python 3.11 + PyGObject |
| UI definitions | Blueprint (`.blp` → `.ui` → GResource) |
| Audio | GStreamer 1.22, `playbin3` |
| HTTP | `aiohttp` with SQLite cache |
| Discovery | Raw UDP socket + `python-zeroconf` fallback |
| Secrets | libsecret via Secret-1 GIR |
| Prefs | GSettings |
| D-Bus / MPRIS | `dbus-next` (asyncio-friendly) |
| Build | Meson + Ninja |
| Distribution | Flatpak, runtime `org.gnome.Platform//50` |
| aarch64 | QEMU userspace via `binfmt_misc`, same workflow as Clicker |

---

## Project layout

```
jamjar/
├── meson.build
├── CLAUDE.md                        # this file
├── jamjar-design.md                 # full architecture doc
├── ROADMAP.md                       # tier ranking and rationale
├── TODO.md                          # current backlog
├── README.md
├── flatpak/
│   └── land.rob.Jamjar.yml
├── data/
│   ├── land.rob.Jamjar.desktop.in
│   ├── land.rob.Jamjar.metainfo.xml.in
│   ├── land.rob.Jamjar.gschema.xml
│   ├── icons/hicolor/
│   │   ├── scalable/apps/land.rob.Jamjar.svg
│   │   ├── scalable/actions/heart-outline-symbolic.svg
│   │   └── symbolic/apps/land.rob.Jamjar-symbolic.svg
│   ├── screenshots/                 # referenced by metainfo.xml.in
│   └── ui/                          # Blueprint sources → .ui → GResource
│       ├── window.blp
│       ├── server-row.blp
│       ├── login-dialog.blp
│       ├── library-page.blp
│       ├── album-page.blp
│       ├── artist-page.blp
│       ├── playlist-page.blp
│       ├── home-page.blp
│       ├── search-page.blp
│       ├── now-playing-bar.blp
│       ├── now-playing-page.blp
│       ├── queue-pane.blp
│       ├── prefs.blp
│       └── help-overlay.blp         # Gtk.ShortcutsWindow
├── po/
└── jamjar/
    ├── __init__.py
    ├── main.py                      # Application entry
    ├── application.py               # Adw.Application subclass + signals
    ├── window.py                    # Main window, breakpoints, help overlay
    ├── discovery.py                 # UDP + Zeroconf
    ├── auth.py                      # Password + Quick Connect
    ├── secrets.py                   # libsecret wrapper
    ├── client.py                    # Async Jellyfin REST client + 401 hook
    ├── models.py                    # Track, Album, Artist, Playlist
    ├── library.py                   # WindowedListModel + per-tab stores
    ├── imagecache.py                # On-disk cover/artist art cache (LRU)
    ├── sleep_timer.py               # Countdown + linear fade-out
    ├── player.py                    # GStreamer pipeline
    ├── queue.py                     # Play queue + shuffle/repeat
    ├── mpris.py                     # MPRIS2 D-Bus
    ├── scrobble.py                  # /Sessions/Playing reporting
    ├── lyrics.py                    # /Items/{id}/Lyrics
    ├── offline.py                   # Sync/download manager (placeholder)
    └── views/
        ├── _common.py               # shared helpers (toasts, links, hearts)
        ├── home.py
        ├── library.py
        ├── album.py
        ├── artist.py
        ├── playlist.py
        ├── search.py
        ├── now_playing.py
        ├── queue.py
        ├── prefs.py
        ├── login.py
        ├── track_menu.py            # right-click context menu (tracks)
        ├── album_menu.py            # right-click context menu (albums)
        └── sleep_timer.py           # sleep-timer Adw.AlertDialog
```

---

## Conventions

### Code style

- Python 3.11+, type hints encouraged on public APIs
- 4-space indent, `snake_case` for funcs/vars, `PascalCase` for classes
- `from gi.repository import ...` after the matching `gi.require_version()` call
- One `Adw`/`Gtk` widget class per file when files start to grow past ~200 lines
- Use `dataclass` for plain data carriers (`Server`, `Track`, `AuthResult`); keep them in `models.py` when they're shared across modules

### GObject

- **Every `GObject.Object` subclass needs `__gtype_name__`.** PyGObject silently breaks signals if you forget. This bit Clicker hard.
- Custom signals registered via `__gsignals__` dict
- `GObject.Property` for properties that need to participate in bindings (e.g., the player's position, current track)

### Async pattern

The Jellyfin client and MPRIS service share a **single asyncio event loop running on a dedicated background thread**. Critical detail:

```python
def _run(self):
    asyncio.set_event_loop(self.loop)   # MUST be on the bg thread
    self.loop.run_forever()
```

Without `set_event_loop` on the worker thread, `asyncio.run_coroutine_threadsafe` will misbehave subtly. Marshal results back to GTK with `GLib.idle_add` — never touch widgets from the worker thread.

### Blueprint over raw XML

UI is authored in `.blp` Blueprint syntax and compiled to `.ui` at build time, then bundled as GResources. Don't write GtkBuilder XML by hand.

### GSettings, not config files

Schema lives in `data/land.rob.Jamjar.gschema.xml`. Anything user-tweakable goes through GSettings: theme, default codec, max bitrate (Wi-Fi vs cellular split), sleep timer default, last-used server ID, equalizer preset, repeat/shuffle state.

### Secrets

Tokens go through `secrets.py` (libsecret wrapper). Never write `AccessToken` to GSettings, a config file, the cache, or stdout in production code paths. Schema attributes: `server_id`, `user_id`.

### Paths

- User data: `GLib.get_user_data_dir() / "jamjar"` — writable, e.g. cert files for Android-TV–style pairing if we ever add it
- User cache: `GLib.get_user_cache_dir() / "jamjar"` — covers, audio downloads, response cache
- `PKGDATADIR` is **read-only** inside Flatpak. Never try to write there. (Clicker hit this with Android TV cert files.)

### Logging

`import logging` with a module-level `log = logging.getLogger(__name__)`. Levels: `DEBUG` for protocol traces, `INFO` for lifecycle, `WARNING` for recoverable issues, `ERROR` for things the user might need to know about. Configure root logger in `main.py`.

---

## Gotchas (lessons carried over from Clicker)

These are real bugs that ate hours on the Clicker project. Re-reading before making the same kind of change saves time:

1. **`__gtype_name__` on every GObject subclass.** PyGObject silently breaks signals without it.
2. **`conf.set_quoted()` vs `conf.set()` in meson `.in` templates.** Use `set_quoted` for string paths/IDs; `set` for booleans and numbers. Mismatching them produces unquoted strings in generated Python and runtime errors.
3. **`asyncio.set_event_loop(loop)` on the background thread** before `run_forever()`. Without it, `run_coroutine_threadsafe` from the GTK thread doesn't reach the loop reliably.
4. **GTK4 removed `set_hscrollbar_policy()` / `set_vscrollbar_policy()`.** Use the two-arg `set_policy(h, v)` only.
5. **`PKGDATADIR` is read-only in Flatpak.** Anything writable goes under `GLib.get_user_data_dir()` or `get_user_cache_dir()`.
6. **`flatpak-pip-generator` quirks:**
   - Target `org.gnome.Sdk` (not freedesktop), since we're on the GNOME runtime
   - Module name uses underscore-joined package list: `python3-aiohttp_python_zeroconf_dbus_next`
   - The post-completion crash from the generator script is harmless; the JSON output is fine
7. **D-pad / button auto-repeat races** (relevant for the player's transport buttons too) — single `pressed` boolean to gate the repeat loop, not a counter.
8. **Test at 360×720 early.** Adaptive bugs almost never show up at desktop widths and are obvious in portrait phone. Use `Adw.Breakpoint` with `max-width: 600sp`.
9. **`flatpak-builder --arch=aarch64`** under QEMU works but is slow. For iteration, build x86_64 locally and only cross-build for release tags.

---

## Authentication flow summary

Both flows produce `(AccessToken, UserId, ServerId)` which gets stored in libsecret keyed by `(server_id, user_id)`.

Every authenticated request needs:

```
Authorization: MediaBrowser Client="Jamjar", Device="<hostname>",
               DeviceId="<stable-uuid>", Version="<x.y.z>",
               Token="<token-or-empty>"
```

`DeviceId` is a UUID generated on first run, stored in GSettings, never regenerated.

### Quick Connect (preferred on phone)

1. `POST /QuickConnect/Initiate` → returns `{Secret, Code}`. Show `Code` big in the UI.
2. Poll `GET /QuickConnect/Connect?Secret=...` every 3s until `Authenticated: true`.
3. `POST /Users/AuthenticateWithQuickConnect` with `{Secret}` → returns the auth payload.

### Password

`POST /Users/AuthenticateByName` with `{Username, Pw}` → auth payload.

The login dialog shows server-discovery results as `Adw.ActionRow`s, then offers Quick Connect by default with a "Use password instead" toggle. On phone widths the dialog goes full-screen.

---

## Jellyfin endpoints in use

| Purpose | Method | Path |
|---|---|---|
| User views | `GET` | `/UserViews` |
| Recently added | `GET` | `/Users/{u}/Items/Latest?IncludeItemTypes=Audio` |
| Albums | `GET` | `/Items?IncludeItemTypes=MusicAlbum&Recursive=true` |
| Artists (album-artists only) | `GET` | `/Artists/AlbumArtists` |
| Album tracks | `GET` | `/Items?ParentId={albumId}&SortBy=ParentIndexNumber,IndexNumber` |
| Playlists | `GET` | `/Items?IncludeItemTypes=Playlist` |
| Search | `GET` | `/Search/Hints?searchTerm=...&IncludeItemTypes=Audio,MusicAlbum,MusicArtist` |
| Recently played | `GET` | `/Items?IncludeItemTypes=Audio&SortBy=DatePlayed&SortOrder=Descending&Filters=IsPlayed` |
| Suggestions | `GET` | `/Users/{u}/Suggestions?mediaType=Audio` |
| Item by id | `GET` | `/Users/{u}/Items/{id}` (used for click-through nav from track artists/albums) |
| Stream URL | `GET` | `/Audio/{id}/universal?api_key=...&audioCodec=...&maxStreamingBitrate=...` |
| Cover art | `GET` | `/Items/{id}/Images/Primary?maxWidth=512&tag={tag}` |
| Lyrics | `GET` | `/Audio/{id}/Lyrics` |
| Report start | `POST` | `/Sessions/Playing` |
| Report progress | `POST` | `/Sessions/Playing/Progress` |
| Report stop | `POST` | `/Sessions/Playing/Stopped` |
| Mark favorite | `POST/DELETE` | `/Users/{u}/FavoriteItems/{id}` |

For offline downloads, use `/Audio/{id}/universal?static=true` to bypass transcoding.

**Library list queries** all pass `EnableTotalRecordCount=false` (Jellyfin's count pass is expensive on big libraries) and use `WindowedListModel` (`library.py`) for on-demand pagination — first page 50 items, subsequent pages 200, with a 40-item lookahead trigger. The model exposes `set_filter(name_starts_with=..., name_less_than=...)` for jump-to-letter, a generation counter for stale-fetch suppression, and a `load-state-changed` signal so views can show empty states only after a definitive empty load.

**Client 401 handling.** `JellyfinClient` accepts an `on_unauthorized` callback that fires on any 401; the application wires this to a single dispatcher that clears the libsecret token, detaches the session, shows a toast, and re-presents the login dialog (deduped via `client is None` guard so parallel 401s collapse to one re-prompt).

---

## Adaptive UI rules

- Top-level container: `Adw.OverlaySplitView` with a breakpoint at `max-width: 600sp` that collapses the sidebar.
- Window minimums: `width-request: 320`, `height-request: 480` — fits FLX1s portrait.
- Two Now Playing presentations:
  - **Compact bar** (phone, narrow desktop): pinned bottom, expands into a full page on tap
  - **Expanded panel** (desktop wide): full-bleed cover + metadata + scrubber + togglable queue
- Phone full-page Now Playing uses `Adw.Carousel` to swipe between Cover, Lyrics, Queue
- Library views: `GtkGridView` for Albums/Artists; `GtkColumnView` on desktop / `GtkListView` of `Adw.ActionRow`s on phone for Songs

---

## Playback rules

- Use `playbin3` — it gives gapless and network buffering for free.
- Gapless: subscribe to the `about-to-finish` bus message and set the next URI before EOS fires.
- ReplayGain: `rgvolume` element in a custom `audio-filter` bin, gated by GSettings.
- Default codec/bitrate split:
  - Wi-Fi: lossless or `audioCodec=copy`
  - Mobile data: 128k Opus
- Keep app alive while playing: `Gtk.Application.hold()` while a track is playing, `release()` on stop.
- "Close window" stops the player and releases the hold so the process exits cleanly. Desktop convention is X = quit (Spotify/Audacious/Rhythmbox all do this); don't reintroduce minimize-on-close. If Phosh backgrounding is wanted, it should be opt-in via GSettings.

---

## System integration

- **MPRIS2** at `org.mpris.MediaPlayer2.land.rob.Jamjar` — required, not optional. This is what makes media keys, GNOME Shell quick settings, and the Phosh lock-screen controls work.
- **Media key fallback** via `Gtk.EventControllerKey` on the window for compositors that don't pick up MPRIS.
- **GNOME Shell search provider** (`org.gnome.Shell.SearchProvider2`) — small effort, nice polish. Lets users hit Super and type a song title.
- **`Gio.Notification`** for "Now Playing" with action buttons (Pause, Next).
- **Scrobbling**: `/Sessions/Playing` on start, `/Sessions/Playing/Progress` every 10s, `/Sessions/Playing/Stopped` on stop. Position is in **ticks** (1 tick = 100 ns).

---

## Flatpak finish-args

```yaml
finish-args:
  - --share=network
  - --share=ipc
  - --socket=fallback-x11
  - --socket=wayland
  - --socket=pulseaudio
  - --device=dri
  - --talk-name=org.freedesktop.secrets
  - --own-name=org.mpris.MediaPlayer2.land.rob.Jamjar
  - --own-name=org.gnome.Shell.SearchProvider.Jamjar
  - --filesystem=xdg-music:ro
```

Notable: `--talk-name=org.freedesktop.secrets` for libsecret access, and the two `--own-name` entries for MPRIS and the search provider.

---

## Roadmap

- **v0.1 (shipped)** — Discovery, both auth flows, library browsing (Albums/Artists/Songs/Playlists), basic playback, queue, MPRIS, scrobbling.
- **v0.2 (in progress)** — Search ✅, lyrics ✅ (with synced highlighting + auto-scroll + click-to-seek), favorites ✅ (with cross-surface sync), sleep timer ✅, volume slider ✅, jump-to-letter ✅, image cache ✅, paginated library ✅, recently played + suggestions ✅, track + album context menus ✅, clickable artist/album labels ✅, GNOME HIG polish (shortcuts overlay, symbolic icon, header search, empty states, error toasts, screenshots/branding in metainfo) ✅. Still open: drag-to-reorder queue, playlist editing, sort/filter on Library pages, up-next preview popover, dedicated history page, JSON cache + manual refresh.
- **v0.3** — Offline downloads, multi-server switching, ReplayGain UI, ListenBrainz passthrough, Phosh lockscreen artwork polish, GNOME Shell search provider.
- **v0.4** — Cast support (UPnP/DLNA via `gupnp`), "driving" view for the NexDock dock, smart playlists, genre/era/mood radio.

---

## Working with Claude Code on this repo

- The design doc (`jamjar-design.md`) is the source of truth for architecture. If a requested change conflicts with it, flag the conflict and ask before diverging.
- Prefer **small, focused commits** that touch one concern at a time (e.g., "discovery: add mDNS fallback" not "discovery + auth + window changes").
- For new modules, follow the layout above. New views go under `jamjar/views/`, with a matching `.blp` under `data/ui/`.
- When adding a Python dep, also add it to the `flatpak-pip-generator` input list and regenerate the JSON. Don't hand-edit the generated JSON.
- Run a quick sanity build (`meson setup build && ninja -C build`) before declaring a change complete. For UI changes, also do a Flatpak build at least once before merging.
- Keep `jamjar-design.md` updated when architectural decisions change. CLAUDE.md should stay short and conventions-focused; longer prose belongs in `jamjar-design.md`.

---

## Naming history

The project was briefly called **Aria**, then renamed to **Jamjar** because the Aria name was already taken in the Linux music player space. The jar metaphor fits a curated music collection well, sits comfortably alongside modern playful GNOME app names (Loupe, Snapshot, Showtime, Decibels), and the `land.rob.Jamjar` app ID is unique across Flathub and AppStream as of project start.

---

*Credit to Claude (claude.ai) for help with the initial design and architecture.*
