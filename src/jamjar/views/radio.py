"""Radio page — pick a station by mood, decade, style or tag."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from gi.repository import Adw, GLib, Gtk

from ..radio import Station, build_stations

if TYPE_CHECKING:
    from ..application import JamjarApplication
    from ..window import JamjarWindow

log = logging.getLogger(__name__)

# Section order on the page, most opinionated first: the curated groupings
# are the reason to visit, the raw genre list is the long tail.
SECTIONS: list[tuple[tuple[str, ...], str]] = [
    (("favorites", "surprise"), "Start Here"),
    (("mood",),                 "Moods"),
    (("decade",),               "Decades"),
    (("style",),                "Styles"),
    (("tag",),                  "From Your Tags"),
]


@Gtk.Template(resource_path="/land/rob/jamjar/radio-page.ui")
class RadioPage(Adw.NavigationPage):
    __gtype_name__ = "JamjarRadioPage"

    sidebar_toggle = Gtk.Template.Child()
    radio_refresh  = Gtk.Template.Child()
    radio_stack    = Gtk.Template.Child()
    sections_box   = Gtk.Template.Child()

    def __init__(self, app: JamjarApplication, window: JamjarWindow) -> None:
        super().__init__()
        self.app = app
        self.window = window
        self.sidebar_toggle.connect("clicked", lambda *_: window.toggle_sidebar())
        self.radio_refresh.connect("clicked", lambda *_: self._load())
        self._loading = False
        GLib.idle_add(self._load)

    # ------- loading -------

    def _load(self) -> bool:
        if self.app.client is None or self._loading:
            return False
        self._loading = True
        if not list(self.sections_box):
            self.radio_stack.set_visible_child_name("loading")

        async def runme():
            client = self.app.client
            genres, years = await client.item_filters("Audio")
            tags = await client.item_tags("Audio")
            return genres, years, tags

        def done(future):
            self._loading = False
            try:
                genres, years, tags = future.result()
            except Exception as e:
                log.warning("station vocabulary fetch failed: %s", e)
                GLib.idle_add(self._show_error)
                return
            GLib.idle_add(self._render, build_stations(genres, years, tags))

        self.app.runner.submit(runme()).add_done_callback(done)
        return False

    def _show_error(self) -> bool:
        # Favorites and Surprise Me need no vocabulary, but a failed fetch
        # means the server is unreachable, so they wouldn't work either.
        self.radio_stack.set_visible_child_name("error")
        return False

    # ------- rendering -------

    def _render(self, stations: list[Station]) -> bool:
        for child in list(self.sections_box):
            self.sections_box.remove(child)

        for kinds, title in SECTIONS:
            matching = [s for s in stations if s.kind in kinds]
            if matching:
                self.sections_box.append(self._section(title, matching))

        self.radio_stack.set_visible_child_name("stations")
        return False

    def _section(self, title: str, stations: list[Station]) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        heading = Gtk.Label(label=title, xalign=0)
        heading.add_css_class("heading")
        box.append(heading)

        flow = Gtk.FlowBox(
            selection_mode=Gtk.SelectionMode.NONE,
            homogeneous=True,
            column_spacing=12,
            row_spacing=12,
            # 2 across at 360 px portrait, more as the window widens.
            min_children_per_line=2,
            max_children_per_line=6,
        )
        for station in stations:
            flow.append(self._card(station))
        box.append(flow)
        return box

    def _card(self, station: Station) -> Gtk.Widget:
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6,
                          margin_top=12, margin_bottom=12,
                          margin_start=6, margin_end=6)
        icon = Gtk.Image.new_from_icon_name(station.icon)
        icon.set_pixel_size(32)
        content.append(icon)

        title = Gtk.Label(label=station.title, ellipsize=3, max_width_chars=16)
        title.add_css_class("heading")
        content.append(title)

        if station.subtitle:
            subtitle = Gtk.Label(label=station.subtitle, ellipsize=3,
                                 max_width_chars=18)
            subtitle.add_css_class("dim-label")
            subtitle.add_css_class("caption")
            content.append(subtitle)

        button = Gtk.Button(child=content, tooltip_text=f"Play {station.title} radio")
        button.add_css_class("card")
        button.connect("clicked", lambda _b: self._start(station))
        return button

    def _start(self, station: Station) -> None:
        if self.app.radio is None:
            return
        self.app.radio.start(station)
        if self.app.show_toast:
            self.app.show_toast(f"Playing {station.title} radio")
