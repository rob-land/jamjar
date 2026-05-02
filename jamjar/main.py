"""Entry point for `jamjar`."""

from __future__ import annotations

import logging
import os
import sys

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gio


def _configure_logging() -> None:
    level_name = os.environ.get("JAMJAR_LOG", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )


def _load_resources() -> None:
    """Load the GResource bundle (looked up via PKGDATADIR injected at build time)."""
    try:
        from . import build_config
        path = os.path.join(build_config.PKGDATADIR, "jamjar.gresource")
    except ImportError:
        # Running uninstalled; fall back to ./build/data/ui/jamjar.gresource
        guess = os.path.join(os.path.dirname(__file__), "..", "build", "data", "ui", "jamjar.gresource")
        path = os.path.abspath(guess)

    if os.path.exists(path):
        resource = Gio.Resource.load(path)
        Gio.Resource._register(resource)
    else:
        logging.getLogger(__name__).warning(
            "GResource not found at %s; UI templates will fail to load", path
        )


def main() -> int:
    _configure_logging()
    _load_resources()
    from .application import JamjarApplication
    app = JamjarApplication()
    return app.run(sys.argv)


if __name__ == "__main__":
    sys.exit(main())
