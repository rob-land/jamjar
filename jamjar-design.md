# Jamjar — A Jellyfin Music Client for GNOME & Phosh

A full-featured GTK4/libadwaita music app that runs adaptively on Linux phones (FuriOS/Phosh) and GNOME desktops, with Jellyfin as the backend. Discovers servers automatically over UDP/mDNS and supports both Quick Connect and username/password authentication.

App ID suggestion: `land.rob.Jamjar` (placeholder — name as you like; "Jamjar", "Cantata", "Sonata", "Prelude" all fit the namespace).

---

## 1. Stack

| Layer | Choice | Rationale |
|---|---|---|
| UI toolkit | GTK 4.14+ / libadwaita 1.5+ | Adaptive primitives, native GNOME/Phosh feel |
| Language | Python 3.11 + PyGObject | Matches Clicker, fastest iteration |
| UI definitions | Blueprint (`.blp` → `.ui` → GResource) | More readable than raw XML |
| Audio | GStreamer 1.22 with `playbin3` | Gapless, network streaming, ReplayGain |
| HTTP | `aiohttp` | Async; bridges to GLib via the asyncio-on-bg-thread pattern |
| Discovery | Raw UDP socket + `python-zeroconf` fallback | Jellyfin broadcasts on UDP/7359 |
| Secrets | libsecret via Secret-1 GIR | Tokens in the keyring, never on disk |
| Prefs | GSettings | Last server, theme, bitrate, etc. |
| Build | Meson + Ninja | Same as Clicker |
| Distribution | Flatpak (`org.gnome.Platform//50`) | aarch64 cross-build via QEMU as you've done before |
| System integration | MPRIS2, GLib search provider | Media keys, lock-screen controls, GNOME Shell search |

---

## 2. Project Layout

```
jamjar/
├── meson.build
├── flatpak/
│   └── land.rob.Jamjar.yml
├── data/
│   ├── land.rob.Jamjar.desktop.in
│   ├── land.rob.Jamjar.metainfo.xml.in
│   ├── land.rob.Jamjar.gschema.xml
│   ├── icons/
│   │   └── hicolor/scalable/apps/land.rob.Jamjar.svg
│   └── ui/                          # Blueprint sources
│       ├── window.blp
│       ├── server-row.blp
│       ├── login-dialog.blp
│       ├── library-page.blp
│       ├── album-page.blp
│       ├── now-playing.blp
│       ├── queue-pane.blp
│       └── prefs.blp
├── po/
└── jamjar/
    ├── __init__.py
    ├── main.py                      # Application entry
    ├── application.py               # Adw.Application subclass
    ├── window.py                    # Main window, breakpoints
    ├── discovery.py                 # UDP + Zeroconf
    ├── auth.py                      # Password + Quick Connect
    ├── secrets.py                   # libsecret wrapper
    ├── client.py                    # Async Jellyfin REST client
    ├── models.py                    # Track, Album, Artist, Playlist
    ├── library.py                   # Caching, paginated lists
    ├── player.py                    # GStreamer pipeline
    ├── queue.py                     # Play queue + shuffle/repeat
    ├── mpris.py                     # MPRIS2 D-Bus
    ├── scrobble.py                  # /Sessions/Playing reporting
    ├── lyrics.py                    # /Items/{id}/Lyrics
    ├── offline.py                   # Sync/download manager
    └── views/                       # Page widgets
        ├── home.py
        ├── library.py
        ├── album.py
        ├── artist.py
        ├── playlist.py
        ├── search.py
        ├── now_playing.py
        ├── queue.py
        └── prefs.py
```

---

## 3. Server Discovery

Jellyfin servers listen for UDP broadcasts on **port 7359** and respond with JSON containing `{Address, Id, Name}`. The app should also support manual entry and mDNS (`_jellyfin._tcp` is published by some setups via Avahi).

```python
# discovery.py
import asyncio, json, socket
from dataclasses import dataclass

@dataclass
class Server:
    name: str
    address: str       # http(s)://host:port
    server_id: str
    source: str        # "udp" | "mdns" | "manual"

class DiscoveryProtocol(asyncio.DatagramProtocol):
    def __init__(self, on_found):
        self.on_found = on_found

    def datagram_received(self, data, addr):
        try:
            payload = json.loads(data.decode())
            self.on_found(Server(
                name=payload["Name"],
                address=payload["Address"],
                server_id=payload["Id"],
                source="udp",
            ))
        except (ValueError, KeyError):
            pass

async def discover_udp(timeout=2.0):
    found, results = set(), []
    loop = asyncio.get_running_loop()
    def collect(srv):
        if srv.server_id not in found:
            found.add(srv.server_id)
            results.append(srv)

    transport, _ = await loop.create_datagram_endpoint(
        lambda: DiscoveryProtocol(collect),
        local_addr=("0.0.0.0", 0),
        allow_broadcast=True,
    )
    transport.sendto(b"Who is JellyfinServer?", ("255.255.255.255", 7359))
    try:
        await asyncio.sleep(timeout)
    finally:
        transport.close()
    return results
```

The UI presents an `Adw.PreferencesGroup` of `Adw.ActionRow`s, one per discovered server, plus a "Connect manually" row. Re-scan is a refresh button in the header. On a phone, broadcast traffic is sometimes dropped on cellular but works fine on Wi-Fi — fall back to mDNS via `python-zeroconf` browsing `_jellyfin._tcp.local.` if UDP returns nothing in 3 seconds.

---

## 4. Authentication

Both flows produce an **AccessToken** + **UserId** that get stored in libsecret keyed by `(server_id, user_id)`.

### 4.1 Authorization header

Every authenticated request needs this header:

```
Authorization: MediaBrowser Client="Jamjar", Device="FLX1s",
               DeviceId="<stable-uuid>", Version="0.1.0",
               Token="<access-token-or-empty>"
```

`DeviceId` should be a stable UUID generated on first run and stored in GSettings. `Device` can come from `GLib.get_host_name()`.

### 4.2 Username / password

```python
async def login_password(self, username: str, password: str) -> AuthResult:
    async with self.session.post(
        f"{self.base}/Users/AuthenticateByName",
        headers=self._auth_header(token=""),
        json={"Username": username, "Pw": password},
    ) as r:
        r.raise_for_status()
        data = await r.json()
        return AuthResult(
            access_token=data["AccessToken"],
            user_id=data["User"]["Id"],
            server_id=data["ServerId"],
        )
```

### 4.3 Quick Connect

A user opens Jellyfin in any browser/client where they're already signed in, goes to their profile → Quick Connect, and types a 6-character code. The flow on our side:

```python
async def quick_connect(self, on_code) -> AuthResult:
    # 1. Initiate
    async with self.session.post(
        f"{self.base}/QuickConnect/Initiate",
        headers=self._auth_header(token=""),
    ) as r:
        r.raise_for_status()
        init = await r.json()       # { Secret, Code, ... }

    on_code(init["Code"])           # show big code in UI

    # 2. Poll until Authenticated == True (or user cancels)
    while True:
        await asyncio.sleep(3)
        async with self.session.get(
            f"{self.base}/QuickConnect/Connect",
            params={"Secret": init["Secret"]},
            headers=self._auth_header(token=""),
        ) as r:
            r.raise_for_status()
            state = await r.json()
            if state.get("Authenticated"):
                break

    # 3. Exchange the secret for a token
    async with self.session.post(
        f"{self.base}/Users/AuthenticateWithQuickConnect",
        headers=self._auth_header(token=""),
        json={"Secret": init["Secret"]},
    ) as r:
        r.raise_for_status()
        data = await r.json()
        return AuthResult(data["AccessToken"], data["User"]["Id"], data["ServerId"])
```

The dialog displays the code in a big monospace label with a copy button, plus a "Use password instead" link that swaps the dialog content. On phone-width breakpoints the code occupies the full width; on desktop it sits centered with secondary instructions beside it.

### 4.4 Token storage

```python
# secrets.py
import gi; gi.require_version("Secret", "1")
from gi.repository import Secret

SCHEMA = Secret.Schema.new(
    "land.rob.Jamjar",
    Secret.SchemaFlags.NONE,
    {"server_id": Secret.SchemaAttributeType.STRING,
     "user_id":   Secret.SchemaAttributeType.STRING},
)

async def store_token(server_id, user_id, token):
    Secret.password_store(
        SCHEMA, {"server_id": server_id, "user_id": user_id},
        Secret.COLLECTION_DEFAULT,
        f"Jamjar token for {user_id}@{server_id}",
        token, None, lambda *_: None,
    )
```

---

## 5. API Client

A single `JellyfinClient` wraps `aiohttp.ClientSession` and exposes typed coroutines. All endpoints are documented at `https://api.jellyfin.org/`.

The endpoints actually used:

| Purpose | Method | Path |
|---|---|---|
| User views | `GET` | `/UserViews` |
| Recently added | `GET` | `/Users/{u}/Items/Latest?IncludeItemTypes=Audio` |
| Albums | `GET` | `/Items?IncludeItemTypes=MusicAlbum&Recursive=true` |
| Artists | `GET` | `/Artists` |
| Album tracks | `GET` | `/Items?ParentId={albumId}&SortBy=ParentIndexNumber,IndexNumber` |
| Playlists | `GET` | `/Items?IncludeItemTypes=Playlist` |
| Search | `GET` | `/Search/Hints?searchTerm=...&IncludeItemTypes=Audio,MusicAlbum,MusicArtist` |
| Stream URL | `GET` | `/Audio/{id}/universal?api_key=...&audioCodec=...&maxStreamingBitrate=...` |
| Cover art | `GET` | `/Items/{id}/Images/Primary?maxWidth=512&tag={tag}` |
| Lyrics | `GET` | `/Audio/{id}/Lyrics` |
| Report start | `POST` | `/Sessions/Playing` |
| Report progress | `POST` | `/Sessions/Playing/Progress` |
| Report stop | `POST` | `/Sessions/Playing/Stopped` |
| Mark favorite | `POST/DELETE` | `/Users/{u}/FavoriteItems/{id}` |

The async loop runs on a **dedicated background thread** (same pattern Clicker uses for asyncio):

```python
# client.py
import asyncio, threading
class AsyncRunner:
    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self):
        asyncio.set_event_loop(self.loop)   # the gotcha you hit in Clicker
        self.loop.run_forever()

    def submit(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self.loop)
```

Results are marshalled back to the GTK main loop with `GLib.idle_add` before touching widgets.

---

## 6. Caching

Three tiers, all under `GLib.get_user_cache_dir() / "jamjar"`:

1. **HTTP response cache** (`responses/`) — `aiohttp-client-cache` with SQLite backend; 1-hour TTL for library lists, 24-hour for static metadata.
2. **Image cache** (`covers/`) — keyed by `{itemId}-{tag}-{size}.jpg`. Use `Gdk.Texture.new_from_file()` and let `GtkPicture` lazy-load. Tags from Jellyfin invalidate the entry when art changes.
3. **Audio cache** (`audio/`) — only when the user explicitly downloads. Stored as the original codec when possible. A small SQLite index tracks `(item_id, path, size, last_played)` for LRU eviction.

For offline mode, the user toggles "Make available offline" on an album/playlist; a `GTask` walks the track list and downloads via `/Audio/{id}/universal?static=true` (no transcode). When the device reports offline (NetworkMonitor), `player.py` rewrites stream URLs to local file paths.

---

## 7. Playback Engine

`playbin3` handles network buffering, gapless, and most format quirks for free.

```python
# player.py
import gi; gi.require_version("Gst", "1.0")
from gi.repository import Gst, GLib, GObject
Gst.init(None)

class Player(GObject.Object):
    __gsignals__ = {
        "position-changed": (GObject.SignalFlags.RUN_FIRST, None, (float,)),
        "track-changed":    (GObject.SignalFlags.RUN_FIRST, None, (object,)),
        "state-changed":    (GObject.SignalFlags.RUN_FIRST, None, (str,)),
    }

    def __init__(self, queue):
        super().__init__()
        self.queue = queue
        self.pipeline = Gst.ElementFactory.make("playbin3", "player")
        self.pipeline.set_property("flags", 0x0002 | 0x0010)  # AUDIO | SOFT_VOLUME
        bus = self.pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message::eos",         self._on_eos)
        bus.connect("message::error",       self._on_error)
        bus.connect("message::about-to-finish", self._on_about_to_finish)
        GLib.timeout_add(500, self._tick)

    def play_track(self, track):
        url = self.queue.client.stream_url(track)
        self.pipeline.set_state(Gst.State.NULL)
        self.pipeline.set_property("uri", url)
        self.pipeline.set_state(Gst.State.PLAYING)
        self.emit("track-changed", track)

    def _on_about_to_finish(self, *_):
        # Gapless: set next URI before EOS
        nxt = self.queue.peek_next()
        if nxt:
            self.pipeline.set_property("uri", self.queue.client.stream_url(nxt))

    def _on_eos(self, *_):
        self.queue.advance()

    def _tick(self):
        ok, pos = self.pipeline.query_position(Gst.Format.TIME)
        if ok:
            self.emit("position-changed", pos / Gst.SECOND)
        return True
```

ReplayGain is added by inserting an `rgvolume` element into a custom `audio-filter` bin. The transcode parameters in the stream URL come from preferences: codec (`opus`/`aac`/`flac`), max bitrate (96k → lossless), and `transcodingContainer`. On Wi-Fi default to lossless or `audioCodec=copy`; on mobile data default to 128k Opus.

---

## 8. Adaptive UI

The window uses `Adw.NavigationSplitView` with an `Adw.Breakpoint` that collapses it below ~600 sp. This is the same shape GNOME Calendar, Files, and Loupe use; on Phosh's portrait window it becomes a simple navigation stack, on a NexDock it becomes a sidebar + content layout.

```blueprint
// data/ui/window.blp
using Gtk 4.0;
using Adw 1;

template $JamjarWindow: Adw.ApplicationWindow {
  default-width: 1100;
  default-height: 720;
  width-request: 320;     // fits a Librem 5 / FLX1s in portrait
  height-request: 480;

  Adw.Breakpoint {
    condition ("max-width: 600sp")
    setters {
      split_view.collapsed: true;
      now_playing_bar.compact: true;
    }
  }

  Adw.OverlaySplitView split_view {
    sidebar-width-fraction: 0.22;
    min-sidebar-width: 200;
    max-sidebar-width: 320;

    sidebar: Adw.NavigationPage {
      title: _("Jamjar");
      Adw.ToolbarView {
        [top] Adw.HeaderBar {}
        ListBox sidebar_list {
          // Home, Albums, Artists, Songs, Playlists, Genres, Downloaded
        }
      }
    };

    content: Adw.NavigationPage {
      Adw.ToolbarView content_view {
        [top] Adw.HeaderBar {}
        // Adw.NavigationView for drilling into Album/Artist/Playlist
        [bottom] $NowPlayingBar now_playing_bar {}
      }
    };
  }
}
```

### 8.1 Now Playing

Two presentations driven by the breakpoint:

- **Compact bar** (phone, narrow desktop): pinned at the bottom of the window. Cover thumbnail, title/artist, play/pause, next. Tapping expands into a full-window page (`Adw.NavigationPage` pushed onto the content stack).
- **Expanded view** (desktop, expanded sidebar pane): full-bleed cover on the left, metadata + scrubber + transport on the right, queue as a third pane togglable from the header. Lyrics tab fades in beside metadata when available.

The expanded full-window page on phone uses `Adw.Carousel` to swipe between Cover, Lyrics, and Queue — this is the pattern Plasma Mobile's Elisa uses and it works well one-handed.

### 8.2 Library views

Albums and Artists use `GtkGridView` with `Gtk.ScrolledWindow.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)` — note the two-arg form; you already hit the Clicker bug where the deprecated single-axis setters don't exist in GTK4. Cover tiles are `GtkPicture` with `content-fit: cover` and a fallback icon (`audio-x-generic-symbolic`) until the texture loads.

Songs view is a `GtkColumnView` on desktop (Title / Artist / Album / Duration columns, sortable) and collapses to a single-column `GtkListView` of `Adw.ActionRow`s on phone widths.

---

## 9. Features Checklist

**Library browsing**: Home (recently played, recently added, suggested), Albums, Artists, Songs, Playlists, Genres, Favorites, Downloaded.

**Search**: Single search entry in the header that hits `/Search/Hints` after a 250ms debounce. Results grouped by type (Tracks, Albums, Artists). On phone, search is its own page reached via the headerbar; on desktop, it's an inline `Gtk.SearchBar` revealer.

**Queue management**: Drag-to-reorder (`GtkListView` + `Gtk.DropTarget`), shuffle (Fisher–Yates over a copy preserving the current item), three repeat modes (off/all/one).

**Playlists**: Create, rename, delete, reorder. Add-to-playlist contextual action on every track via right-click on desktop or long-press → `Adw.ActionRow` menu on phone.

**Lyrics**: `/Audio/{id}/Lyrics` returns timestamped LRC when available. Render with a synced auto-scroll using `Gtk.ScrolledWindow.emit("scroll-child", ...)`; the active line is bold and centered.

**Favorites & rating**: Heart toggle on every track row, hooked to `/Users/{u}/FavoriteItems/{id}`.

**Sleep timer**: GSettings-backed dropdown (15/30/60 min, end of track, end of album).

**Cast / external output**: Surface PulseAudio/Pipewire sinks via `pactl` or the GVC GIR; on a NexDock, audio just follows the default sink. (No AirPlay/Chromecast in v1.)

**Equalizer**: 10-band via `equalizer-10bands` GStreamer element, with presets stored in GSettings.

---

## 10. System Integration

### 10.1 MPRIS2

A D-Bus interface at `org.mpris.MediaPlayer2.land.rob.Jamjar` exposes Play/Pause/Next/Previous and metadata. This is what makes media keys, the GNOME Shell quick settings panel, and the Phosh lock-screen controls all "just work". Implement with `dbus-next` (asyncio-friendly) on the same background loop as the API client.

The two interfaces to implement: `org.mpris.MediaPlayer2` (Identity, DesktopEntry, Quit, Raise) and `org.mpris.MediaPlayer2.Player` (PlaybackStatus, Metadata, Position, properties + Play/Pause/Next/Previous/Seek/SetPosition methods + Seeked signal).

### 10.2 Media keys (fallback)

If MPRIS isn't picked up by the compositor, `Gtk.EventControllerKey` on the window catches `XF86AudioPlay/Stop/Next/Prev` directly.

### 10.3 Background play

On Phosh, the app needs to keep playing when the window is minimized. The Flatpak manifest grants `--talk-name=org.mpris.MediaPlayer2.*` and the app uses `Gtk.Application.hold()` while playing so it isn't reaped. For "exit closes window but keeps playback", override the `close-request` signal and minimize instead, with a "Quit Jamjar" entry in the primary menu for an actual exit.

### 10.4 Notifications & search provider

`Gio.Notification` for "Now Playing" with action buttons (Pause, Next). A GNOME Shell search provider (`org.gnome.Shell.SearchProvider2` D-Bus iface) lets users hit Super and type a song title to jump straight into it — small effort for nice polish.

### 10.5 Scrobbling

POST `/Sessions/Playing` on track start, `/Sessions/Playing/Progress` every 10 seconds with `PositionTicks` (1 tick = 100 ns), `/Sessions/Playing/Stopped` on stop. This drives Jellyfin's "continue listening" + "play count" + any plugin scrobblers (Last.fm, ListenBrainz).

---

## 11. Build & Packaging

### 11.1 meson skeleton

```meson
project('jamjar', 'c',
  version: '0.1.0',
  meson_version: '>= 1.2.0',
  default_options: ['warning_level=2'])

i18n   = import('i18n')
gnome  = import('gnome')
python = import('python').find_installation('python3')

prefix     = get_option('prefix')
bindir     = prefix / get_option('bindir')
datadir    = prefix / get_option('datadir')
pkgdatadir = datadir / meson.project_name()
moduledir  = python.get_install_dir() / 'jamjar'

conf = configuration_data()
conf.set('PYTHON', python.full_path())
conf.set('VERSION', meson.project_version())
conf.set_quoted('PKGDATADIR', pkgdatadir)   # NB: set_quoted, not set
conf.set_quoted('LOCALEDIR', prefix / get_option('localedir'))

subdir('data')
subdir('jamjar')
subdir('po')

gnome.post_install(
  glib_compile_schemas: true,
  gtk_update_icon_cache: true,
  update_desktop_database: true,
)
```

### 11.2 Flatpak manifest

```yaml
# flatpak/land.rob.Jamjar.yml
app-id: land.rob.Jamjar
runtime: org.gnome.Platform
runtime-version: '47'
sdk: org.gnome.Sdk
command: jamjar

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

modules:
  - name: blueprint-compiler
    buildsystem: meson
    cleanup: ['*']
    sources:
      - type: git
        url: https://gitlab.gnome.org/jwestman/blueprint-compiler.git
        tag: v0.16.0

  - python3-modules.json   # generated by flatpak-pip-generator
                            # remember: target org.gnome.Sdk, use the
                            # "python3-aiohttp_python_zeroconf_dbus_next"
                            # underscore-joined install name pattern

  - name: jamjar
    buildsystem: meson
    sources:
      - type: dir
        path: ..
```

aarch64 cross-build via QEMU is identical to the Clicker workflow: `flatpak-builder --arch=aarch64 --install-deps-from=flathub ...` inside a userspace `binfmt_misc`-enabled environment.

---

## 12. Application Skeleton

```python
# jamjar/main.py
import sys
from .application import JamjarApplication

def main():
    app = JamjarApplication()
    return app.run(sys.argv)
```

```python
# jamjar/application.py
import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Adw, Gio, Gtk
from .window import JamjarWindow
from .client import AsyncRunner, JellyfinClient
from .player import Player
from .queue import PlayQueue
from .mpris import MprisService

class JamjarApplication(Adw.Application):
    def __init__(self):
        super().__init__(
            application_id='land.rob.Jamjar',
            flags=Gio.ApplicationFlags.DEFAULT_FLAGS,
        )
        self.runner = AsyncRunner()
        self.client = None
        self.queue  = None
        self.player = None
        self.mpris  = None

    def do_activate(self):
        win = self.props.active_window
        if not win:
            win = JamjarWindow(application=self)
        win.present()

    def do_startup(self):
        Adw.Application.do_startup(self)
        # Resources, actions, accelerators
        self.set_accels_for_action('app.quit',     ['<Primary>q'])
        self.set_accels_for_action('app.search',   ['<Primary>f'])
        self.set_accels_for_action('player.toggle',['space'])
        self.set_accels_for_action('player.next',  ['<Primary>Right'])
        self.set_accels_for_action('player.prev',  ['<Primary>Left'])

    def attach_session(self, client: JellyfinClient):
        """Called after successful login."""
        self.client = client
        self.queue  = PlayQueue(client)
        self.player = Player(self.queue)
        self.mpris  = MprisService(self.player, self.queue)
```

The first run shows a `LoginDialog` (Adw.Dialog) with a server picker that triggers UDP discovery, then either Quick Connect or password auth. After success the dialog closes and the main library view loads.

---

## 13. Roadmap

**v0.1** — Discovery, both auth flows, library browsing (Albums/Artists/Songs/Playlists), basic playback, queue, MPRIS, scrobbling.

**v0.2** — Search, lyrics, favorites, playlist editing, equalizer, sleep timer, GNOME Shell search provider.

**v0.3** — Offline downloads, multi-server switching, ReplayGain UI, ListenBrainz passthrough, Phosh lockscreen artwork polish.

**v0.4** — Cast support (UPnP/DLNA renderers via `gupnp`), CarPlay-style "driving" view for the NexDock, smart playlists.

---

## 14. Things to watch out for (lessons from Clicker)

- Every `GObject.Object` subclass needs `__gtype_name__` or PyGObject silently breaks signals.
- Use `conf.set_quoted('PKGDATADIR', …)` for string paths, plain `conf.set` for booleans/numbers.
- `asyncio.set_event_loop(loop)` must run on the background thread before `run_forever()`.
- For per-user data (auth tokens, downloads, caches), use `GLib.get_user_*_dir()`, never `PKGDATADIR` (read-only inside Flatpak).
- `GtkScrolledWindow.set_policy(h, v)` is the only form in GTK4.
- For the `flatpak-pip-generator` step, target `org.gnome.Sdk` and accept the underscore-joined module name; the post-completion crash is harmless.
- Test on a 360×720 window early — most adaptive bugs surface only at phone widths.
