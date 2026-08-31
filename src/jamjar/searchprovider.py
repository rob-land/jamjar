"""GNOME Shell search provider — find music from the Activities overview.

Shell talks to us over the app's own bus name (the one `Gio.Application`
already owns), so this needs no extra name ownership — just an object on
the connection and an .ini file telling Shell where to look.

Searches are answered asynchronously: Shell's calls are D-Bus method
invocations that can be replied to whenever the Jellyfin request lands,
so nothing blocks the GTK loop waiting on the network.
"""

from __future__ import annotations

import logging

from gi.repository import Gio, GLib

log = logging.getLogger(__name__)

OBJECT_PATH = "/land/rob/jamjar/SearchProvider"

INTERFACE_XML = """
<node>
  <interface name="org.gnome.Shell.SearchProvider2">
    <method name="GetInitialResultSet">
      <arg type="as" name="terms" direction="in"/>
      <arg type="as" name="results" direction="out"/>
    </method>
    <method name="GetSubsearchResultSet">
      <arg type="as" name="previous_results" direction="in"/>
      <arg type="as" name="terms" direction="in"/>
      <arg type="as" name="results" direction="out"/>
    </method>
    <method name="GetResultMetas">
      <arg type="as" name="identifiers" direction="in"/>
      <arg type="aa{sv}" name="metas" direction="out"/>
    </method>
    <method name="ActivateResult">
      <arg type="s" name="identifier" direction="in"/>
      <arg type="as" name="terms" direction="in"/>
      <arg type="u" name="timestamp" direction="in"/>
    </method>
    <method name="LaunchSearch">
      <arg type="as" name="terms" direction="in"/>
      <arg type="u" name="timestamp" direction="in"/>
    </method>
  </interface>
</node>
"""

MAX_RESULTS = 8

ICONS = {
    "Audio":       "audio-x-generic-symbolic",
    "MusicAlbum":  "media-optical-cd-audio-symbolic",
    "MusicArtist": "system-users-symbolic",
}


class SearchProvider:
    """Exports org.gnome.Shell.SearchProvider2 for the application."""

    def __init__(self, app) -> None:
        self.app = app
        self._registration: int | None = None
        # Result id -> SearchHit, so GetResultMetas and ActivateResult
        # don't have to re-query for something we just found.
        self._hits: dict[str, object] = {}

    def register(self, connection: Gio.DBusConnection) -> None:
        info = Gio.DBusNodeInfo.new_for_xml(INTERFACE_XML)
        try:
            self._registration = connection.register_object(
                OBJECT_PATH, info.interfaces[0], self._on_call, None, None)
        except GLib.Error as e:
            log.warning("search provider registration failed: %s", e.message)

    def unregister(self, connection: Gio.DBusConnection) -> None:
        if self._registration is not None:
            connection.unregister_object(self._registration)
            self._registration = None

    # ------- D-Bus dispatch -------

    def _on_call(self, _connection, _sender, _path, _interface,
                 method: str, params, invocation) -> None:
        if method == "GetInitialResultSet":
            self._search(params[0], invocation)
        elif method == "GetSubsearchResultSet":
            self._search(params[1], invocation)
        elif method == "GetResultMetas":
            invocation.return_value(GLib.Variant("(aa{sv})",
                                                 ([self._meta(i) for i in params[0]],)))
        elif method == "ActivateResult":
            self._activate(params[0], params[2])
            invocation.return_value(None)
        elif method == "LaunchSearch":
            self._launch(" ".join(params[0]), params[1])
            invocation.return_value(None)
        else:
            invocation.return_value(None)

    # ------- search -------

    def _search(self, terms: list[str], invocation) -> None:
        query = " ".join(terms).strip()
        client = self.app.client
        if not query or client is None:
            # Not signed in yet: Shell shouldn't show a Jamjar section at
            # all rather than an error.
            invocation.return_value(GLib.Variant("(as)", ([],)))
            return

        async def runme():
            return await client.search(query, limit=MAX_RESULTS)

        def done(future):
            try:
                hits = future.result()
            except Exception as e:
                log.info("shell search failed: %s", e)
                hits = []
            ids = []
            for hit in hits:
                key = f"{hit.type}:{hit.item_id}"
                self._hits[key] = hit
                ids.append(key)
            GLib.idle_add(self._reply, invocation, ids)

        self.app.runner.submit(runme()).add_done_callback(done)

    def _reply(self, invocation, ids: list[str]) -> bool:
        invocation.return_value(GLib.Variant("(as)", (ids,)))
        return False

    def _meta(self, identifier: str) -> dict:
        hit = self._hits.get(identifier)
        kind = identifier.split(":", 1)[0]
        name = getattr(hit, "name", identifier)
        subtitle = getattr(hit, "secondary", "") if hit is not None else ""
        return {
            "id":          GLib.Variant("s", identifier),
            "name":        GLib.Variant("s", name),
            "description": GLib.Variant("s", subtitle),
            "gicon":       GLib.Variant("s", ICONS.get(kind, ICONS["Audio"])),
        }

    # ------- activation -------

    def _activate(self, identifier: str, timestamp: int) -> None:
        hit = self._hits.get(identifier)
        if hit is None:
            self._launch("", timestamp)
            return
        GLib.idle_add(self._open_hit, hit)

    def _open_hit(self, hit) -> bool:
        window = self.app.props.active_window
        if window is None:
            self.app.activate()
            window = self.app.props.active_window
        if window is not None:
            window.present()
            window.open_search_hit(hit)
        return False

    def _launch(self, query: str, _timestamp: int) -> None:
        def present() -> bool:
            self.app.activate()
            window = self.app.props.active_window
            if window is not None:
                window.present()
                window.open_search(query)
            return False

        GLib.idle_add(present)
