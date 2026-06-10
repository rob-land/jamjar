"""Import smoke test for every view module.

Regression cover for the class of bug where a module imports a name from
the wrong sibling — e.g. `from ._common import install_track_menu` when
`install_track_menu` actually lives in `.track_menu`. Both `py_compile`
and `compileall` PASS on that: it's valid syntax. The ImportError only
fires at runtime, the first time the module is loaded — and because
`application.py` imports `window.py` (and thus the views) lazily inside
`do_activate`, "first load" means *app launch* on the target device. A
stale build on another machine masks it entirely. This test forces every
view module to import so the failure surfaces in CI instead of on a phone.

The view classes use `@Gtk.Template(resource_path=...)`, which resolves
the bundled `.ui` at class-definition (import) time, so the compiled
GResource must be registered first. That buys a bonus check: a view whose
`.blp` was never wired into `jamjar.gresource.xml` also fails here. No
display is needed — registering the resource and letting GObject lazily
register the Gtk/Adw types is enough; we deliberately avoid `Gtk.init()`.
"""

from __future__ import annotations

import importlib
import os
import pkgutil
from pathlib import Path

import gi
import pytest

# The launcher (main.py) is the single require_version declaration site at
# runtime; tests import modules directly, so re-declare the same versions
# here before any `from gi.repository import ...` in jamjar modules runs.
gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gst", "1.0")
gi.require_version("Secret", "1")

from gi.repository import Gio  # noqa: E402


def _find_gresource() -> Path | None:
    """Locate the compiled GResource.

    Meson passes its exact path via JAMJAR_GRESOURCE (see tests/meson.build).
    Falling back to a search of the usual build dirs lets a bare
    `PYTHONPATH=src pytest` run work too, as long as a build exists.
    """
    env = os.environ.get("JAMJAR_GRESOURCE")
    if env and Path(env).exists():
        return Path(env)
    root = Path(__file__).resolve().parent.parent
    for build in ("_build", "build", "builddir", "_flatpak_build"):
        cand = root / build / "data" / "ui" / "jamjar.gresource"
        if cand.exists():
            return cand
    return None


_GRESOURCE = _find_gresource()
if _GRESOURCE is not None:
    Gio.Resource._register(Gio.Resource.load(str(_GRESOURCE)))


def _view_modules() -> list[str]:
    import jamjar.views as views

    return sorted(
        f"jamjar.views.{m.name}" for m in pkgutil.iter_modules(views.__path__)
    )


requires_gresource = pytest.mark.skipif(
    _GRESOURCE is None,
    reason="compiled jamjar.gresource not found — build first (ninja -C _build)",
)


@requires_gresource
@pytest.mark.parametrize("module", _view_modules())
def test_view_module_imports(module: str) -> None:
    importlib.import_module(module)


@requires_gresource
def test_window_imports() -> None:
    # The exact chain that motivated this test: do_activate imports window.py,
    # which pulls in every view. Importing it directly is the closest thing to
    # "launch the app" without a display or an event loop.
    importlib.import_module("jamjar.window")
