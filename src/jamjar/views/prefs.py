"""Preferences dialog — bound to GSettings."""

from __future__ import annotations

from gi.repository import Adw, Gio, Gtk


@Gtk.Template(resource_path="/land/rob/jamjar/prefs.ui")
class PreferencesDialog(Adw.PreferencesDialog):
    __gtype_name__ = "JamjarPreferences"

    color_scheme_row   = Gtk.Template.Child()
    replaygain_row     = Gtk.Template.Child()
    sleep_timer_row    = Gtk.Template.Child()
    codec_wifi_row     = Gtk.Template.Child()
    bitrate_wifi_row   = Gtk.Template.Child()
    codec_mobile_row   = Gtk.Template.Child()
    bitrate_mobile_row = Gtk.Template.Child()

    def __init__(self, settings: Gio.Settings) -> None:
        super().__init__()
        self.settings = settings

        self.color_scheme_row.set_selected(settings.get_enum("color-scheme"))
        self.color_scheme_row.connect("notify::selected", self._on_color_scheme)

        settings.bind("replaygain", self.replaygain_row, "active",
                      Gio.SettingsBindFlags.DEFAULT)

        self.sleep_timer_row.set_value(settings.get_uint("sleep-timer-default-minutes"))
        self.sleep_timer_row.connect("changed", self._on_sleep_timer)

        self.codec_wifi_row.set_selected(settings.get_enum("codec-wifi"))
        self.codec_wifi_row.connect("notify::selected", self._on_codec_wifi)

        self.codec_mobile_row.set_selected(settings.get_enum("codec-mobile"))
        self.codec_mobile_row.connect("notify::selected", self._on_codec_mobile)

        self.bitrate_wifi_row.set_value(settings.get_uint("max-bitrate-wifi") // 1000)
        self.bitrate_wifi_row.connect("changed", self._on_bitrate_wifi)

        self.bitrate_mobile_row.set_value(settings.get_uint("max-bitrate-mobile") // 1000)
        self.bitrate_mobile_row.connect("changed", self._on_bitrate_mobile)

    def _on_color_scheme(self, row, _ps) -> None:
        selected = row.get_selected()
        self.settings.set_enum("color-scheme", selected)
        manager = Adw.StyleManager.get_default()
        schemes = {
            0: Adw.ColorScheme.DEFAULT,
            1: Adw.ColorScheme.FORCE_LIGHT,
            2: Adw.ColorScheme.FORCE_DARK,
        }
        manager.set_color_scheme(schemes.get(selected, Adw.ColorScheme.DEFAULT))

    def _on_sleep_timer(self, row) -> None:
        self.settings.set_uint("sleep-timer-default-minutes", int(row.get_value()))

    def _on_codec_wifi(self, row, _ps) -> None:
        self.settings.set_enum("codec-wifi", row.get_selected())

    def _on_codec_mobile(self, row, _ps) -> None:
        self.settings.set_enum("codec-mobile", row.get_selected())

    def _on_bitrate_wifi(self, row) -> None:
        self.settings.set_uint("max-bitrate-wifi", int(row.get_value()) * 1000)

    def _on_bitrate_mobile(self, row) -> None:
        self.settings.set_uint("max-bitrate-mobile", int(row.get_value()) * 1000)
