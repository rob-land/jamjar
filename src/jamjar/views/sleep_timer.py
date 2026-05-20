"""Sleep-timer dialog — preset minutes + custom value, or cancel an active timer."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from gi.repository import Adw, Gtk

if TYPE_CHECKING:
    from ..application import JamjarApplication

log = logging.getLogger(__name__)

PRESET_MINUTES = (15, 30, 45, 60)


class SleepTimerDialog(Adw.AlertDialog):
    __gtype_name__ = "JamjarSleepTimerDialog"

    def __init__(self, app: JamjarApplication) -> None:
        super().__init__()
        self.app = app
        self.set_heading("Sleep Timer")
        if app.sleep_timer.is_active:
            self._build_active_view()
        else:
            self._build_picker_view()
        self.connect("response", self._on_response)

    def _build_active_view(self) -> None:
        remaining = self.app.sleep_timer.remaining_seconds
        mins = (remaining + 59) // 60
        plural = "" if mins == 1 else "s"
        self.set_body(f"Stopping playback in {mins} minute{plural}.")
        self.add_response("close", "Close")
        self.add_response("cancel-timer", "Cancel Timer")
        self.set_response_appearance("cancel-timer",
                                     Adw.ResponseAppearance.DESTRUCTIVE)
        self.set_default_response("close")
        self.set_close_response("close")

    def _build_picker_view(self) -> None:
        self.set_body("Stop playback after…")

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                        margin_top=4, margin_bottom=4)

        presets = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6,
                          halign=Gtk.Align.CENTER)
        for m in PRESET_MINUTES:
            btn = Gtk.Button(label=f"{m} min")
            btn.connect("clicked", self._on_preset_clicked, m)
            presets.append(btn)
        outer.append(presets)

        custom_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6,
                             halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER)
        custom_row.append(Gtk.Label(label="Custom:"))
        spin = Gtk.SpinButton.new_with_range(1, 480, 5)
        spin.set_value(max(1, int(self.app.settings.get_uint("sleep-timer-default-minutes"))))
        spin.set_numeric(True)
        custom_row.append(spin)
        custom_row.append(Gtk.Label(label="min"))
        start_btn = Gtk.Button(label="Start")
        start_btn.add_css_class("suggested-action")
        start_btn.connect("clicked", self._on_custom_start, spin)
        custom_row.append(start_btn)
        outer.append(custom_row)

        self.set_extra_child(outer)
        self.add_response("cancel", "Cancel")
        self.set_close_response("cancel")
        self.set_default_response("cancel")

    def _on_preset_clicked(self, _btn, minutes: int) -> None:
        self._start(minutes)

    def _on_custom_start(self, _btn, spin: Gtk.SpinButton) -> None:
        self._start(int(spin.get_value()))

    def _start(self, minutes: int) -> None:
        if minutes <= 0:
            return
        self.app.sleep_timer.start(minutes)
        self.app.settings.set_uint("sleep-timer-default-minutes", minutes)
        self.close()

    def _on_response(self, _dialog, response: str) -> None:
        if response == "cancel-timer":
            self.app.sleep_timer.cancel()
