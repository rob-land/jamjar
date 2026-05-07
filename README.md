# Jamjar

A native GTK4 / libadwaita Jellyfin music client for **GNOME desktop** and **Phosh**
(Linux phones — particularly the FuriLabs FLX1s).

- App ID: `land.rob.Jamjar`
- Backend: Jellyfin (self-hosted)
- Adaptive: works in portrait phone and docked desktop layouts
- Auth: Quick Connect or username/password, tokens stored in libsecret
- Discovery: UDP broadcast on port 7359 with mDNS fallback
- Audio: GStreamer `playbin3`, gapless, ReplayGain
- System integration: MPRIS2, GNOME Shell search provider, MPRIS-driven media keys

See [`CLAUDE.md`](./CLAUDE.md) for orientation and [`DESIGN.md`](./DESIGN.md)
for the full design.

## How it was made

> ⚠️ **Honest disclosure**: Every line of code in this project was written
> by [Claude.ai](https://claude.ai) (Anthropic's AI assistant). I apologise
> in advance for the AI slop. Pull requests that fix the inevitable weird
> decisions are very welcome.

---

## Requirements

### Build tools

| Tool | Why | Min version |
|---|---|---|
| `meson` | Build system | 1.2 |
| `ninja` | Backend for meson | — |
| `blueprint-compiler` | `.blp` → `.ui` | 0.16 |
| `python3` | Runtime + scripts | 3.11 |
| `pkg-config` | Locate gio-2.0 | — |
| `glib-compile-schemas`, `glib-compile-resources` | Schema + GResource bundles | — |
| `desktop-file-validate`, `appstreamcli` | Used by `meson test` (optional) | — |
| `gettext` (`msgfmt`, `xgettext`) | i18n | — |

### Runtime libraries

| Library | Brought in via | Min version |
|---|---|---|
| GTK | `gi.require_version("Gtk", "4.0")` | 4.14 |
| libadwaita | `gi.require_version("Adw", "1")` | 1.5 |
| libsecret | `gi.require_version("Secret", "1")` | — |
| GStreamer + plugins-base + plugins-good | `gi.require_version("Gst", "1.0")`, `playbin3` | 1.22 |
| PyGObject | Python ↔ GObject bridge | — |

GStreamer needs `gst-plugins-base` for `playbin3` and `gst-plugins-good` (or
`gst-plugins-bad`/`gst-libav`) for the codecs your server transcodes to.
For ReplayGain (`rgvolume`) and the equaliser (`equalizer-10bands`) you also
want `gst-plugins-good` and `gst-plugins-bad`.

### Python packages

| Package | Used by | Required? |
|---|---|---|
| `aiohttp` | `client.py`, `auth.py` | yes |
| `python-zeroconf` | `discovery.py` (mDNS fallback) | recommended |
| `dbus-next` | `mpris.py` | recommended (MPRIS goes silent without it) |

### Distribution package recipes

#### Fedora 41+

```sh
sudo dnf install meson ninja-build blueprint-compiler \
                 gtk4-devel libadwaita-devel libsecret-devel \
                 gstreamer1 gstreamer1-plugins-base gstreamer1-plugins-good \
                 gstreamer1-plugins-bad-free gstreamer1-libav \
                 python3-gobject python3-aiohttp python3-zeroconf \
                 desktop-file-utils appstream gettext \
                 glib2-devel
# dbus-next is on PyPI; install via pip into a venv or as the user:
pip install --user dbus-next
```

#### Debian / Ubuntu

```sh
sudo apt install meson ninja-build blueprint-compiler \
                 libgtk-4-dev libadwaita-1-dev libsecret-1-dev \
                 gstreamer1.0-tools gstreamer1.0-plugins-base \
                 gstreamer1.0-plugins-good gstreamer1.0-plugins-bad \
                 gstreamer1.0-libav python3-gi python3-gi-cairo \
                 python3-aiohttp python3-zeroconf python3-dbus-next \
                 desktop-file-utils appstreamcli gettext
```

#### Arch

```sh
sudo pacman -S meson ninja blueprint-compiler gtk4 libadwaita libsecret \
               gstreamer gst-plugins-base gst-plugins-good gst-plugins-bad \
               gst-libav python-gobject python-aiohttp python-zeroconf \
               python-dbus-next desktop-file-utils appstream gettext
```

---

## Build (host install, system-wide)

```sh
meson setup build --prefix=/usr/local
ninja -C build
sudo meson install -C build
```

After install, `jamjar` is on `$PATH`, and `.desktop` / metainfo / GSettings
schema / icon / GResource bundle land in `$prefix/share`.

To validate the bundled metadata files:

```sh
meson test -C build
```

This runs `desktop-file-validate`, `glib-compile-schemas --strict --dry-run`,
and `appstreamcli validate --no-net` over the configured outputs.

## Build (uninstalled / development)

If you want to run from a worktree without touching system paths, install to a
local prefix and point the runtime at it:

```sh
meson setup build --prefix="$PWD/install-prefix"
ninja -C build
meson install -C build

PFX="$PWD/install-prefix"
PYTHONPATH="$PFX/lib/python3.$(python3 -c 'import sys; print(sys.version_info.minor)')/site-packages" \
GSETTINGS_SCHEMA_DIR="$PFX/share/glib-2.0/schemas" \
XDG_DATA_DIRS="$PFX/share:${XDG_DATA_DIRS:-/usr/share}" \
JAMJAR_LOG=DEBUG \
"$PFX/bin/jamjar"
```

`JAMJAR_LOG` can be `DEBUG`, `INFO`, `WARNING`, `ERROR`. Default is `INFO`.

---

## Flatpak

The Flatpak manifest pulls a pinned `blueprint-compiler` and the Python deps as
a generated module, then builds against the GNOME 50 runtime.

### One-time setup

```sh
flatpak install --user flathub org.gnome.Platform//50 org.gnome.Sdk//50
```

### Generate the Python deps module

`build-aux/flatpak/python3-deps.json` in this tree was generated via the upstream
`flatpak-pip-generator.py` from
[`flatpak-builder-tools`](https://github.com/flatpak/flatpak-builder-tools).
Regenerate it whenever the dep set changes. The generator must run **inside
the GNOME SDK** so its conditional dependencies (e.g. `typing-extensions`
under Python 3.12) match the runtime that flatpak-builder will install into:

```sh
mkdir -p /tmp/jamjar-gen
curl -fsSL -o /tmp/jamjar-gen/flatpak-pip-generator.py \
    https://raw.githubusercontent.com/flatpak/flatpak-builder-tools/master/pip/flatpak-pip-generator.py

flatpak run --command=sh \
    --filesystem=/tmp/jamjar-gen \
    --share=network \
    org.gnome.Sdk//50 -c '
        cd /tmp/jamjar-gen
        python3 -m venv .venv
        .venv/bin/pip install --quiet "requirements-parser>=0.11.0,<1.0.0" "packaging>=23.0"
        .venv/bin/python3 flatpak-pip-generator.py \
            --output python3-deps \
            poetry-core aiohttp zeroconf dbus-next
    '
cp /tmp/jamjar-gen/python3-deps.json build-aux/flatpak/python3-deps.json
```

Notes:

- The PyPI name for the mDNS library is `zeroconf` (not `python-zeroconf`).
- `poetry-core` is listed **first** so it ends up earlier in the manifest —
  it's `zeroconf`'s build backend, and flatpak-builder's offline pip install
  (`--no-build-isolation --no-index`) needs it already on disk before
  `python3-zeroconf` runs.
- If you add a dep that uses `hatchling`, `flit-core`, or `setuptools-scm`
  as its build backend, list those first too.

### Build & install

The repo ships a `build-all.sh` driver that wraps `flatpak-builder`
with sensible defaults; it patches `python3-deps.json` (via
`fix-flatpak-deps.py`) so source tarballs become pre-built wheels and
the build sandbox doesn't need a Rust toolchain.

```sh
./build-all.sh                  # both arches
./build-all.sh --arch x86_64    # single arch
./build-all.sh --regen-deps     # regenerate python3-deps.json from requirements.txt first
./build-all.sh --install        # also installs the host-arch bundle (--user)
```

Or directly:

```sh
flatpak-builder --user --install --force-clean \
    build-flatpak build-aux/flatpak/land.rob.Jamjar.json
flatpak run land.rob.Jamjar
```

### aarch64 cross-build (FLX1s / Phosh)

`./build-all.sh --arch aarch64` (inside a `binfmt_misc`-enabled QEMU
userspace) does the cross-build automatically.

---

## First run

1. Jamjar opens a login dialog.
2. UDP broadcast finds local Jellyfin servers; pick one or "Connect Manually…".
3. Quick Connect is offered first; on a phone this is the path of least
   friction. Type the 6-character code into Jellyfin's profile → Quick Connect
   on a signed-in browser session. The dialog falls back to username/password
   automatically if the server has Quick Connect disabled.
4. Tokens land in libsecret keyed by `(server_id, user_id)`. Subsequent runs
   restore the session silently.

---

## Layout reminder

```
jamjar/
├── meson.build              # root meson, splits string conf vs. python conf
├── build-aux/flatpak/       # manifest + python deps json placeholder
├── data/
│   ├── ui/*.blp             # Blueprint sources, compiled to .ui at build
│   └── *.desktop.in / *.metainfo.xml.in / *.gschema.xml
├── src/jamjar/              # Python package
│   ├── application.py       # Adw.Application; owns runner/client/player/queue
│   ├── window.py            # Main window template + nav
│   ├── client.py            # AsyncRunner (asyncio bg thread) + JellyfinClient
│   ├── player.py            # GStreamer playbin3 wrapper
│   ├── mpris.py             # dbus-next MPRIS2 service
│   ├── views/               # Page widgets, one per .blp template
│   └── …                    # discovery, auth, secrets, library, queue, …
└── po/                      # (no translations yet; .pot generated on demand)
```
