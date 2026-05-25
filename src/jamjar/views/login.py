"""Login dialog — server discovery, Quick Connect, password fallback."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from gi.repository import Adw, Gdk, GLib, GObject, Gtk

from ..auth import Authenticator, AuthError
from ..client import JellyfinClient
from ..discovery import discover
from ..models import Server
from ..secrets import store_token
from ._common import escape_markup

if TYPE_CHECKING:
    from ..application import JamjarApplication

log = logging.getLogger(__name__)


@Gtk.Template(resource_path="/land/rob/jamjar/login-dialog.ui")
class LoginDialog(Adw.Dialog):
    __gtype_name__ = "JamjarLoginDialog"
    __gsignals__ = {
        "authenticated": (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    nav                  = Gtk.Template.Child()
    discovery_status     = Gtk.Template.Child()
    discovered_group     = Gtk.Template.Child()
    rescan_row           = Gtk.Template.Child()
    manual_row           = Gtk.Template.Child()
    address_entry        = Gtk.Template.Child()
    manual_continue      = Gtk.Template.Child()
    quick_connect_view   = Gtk.Template.Child()
    quick_connect_code   = Gtk.Template.Child()
    quick_connect_copy   = Gtk.Template.Child()
    qc_error             = Gtk.Template.Child()
    qc_retry             = Gtk.Template.Child()
    qc_status            = Gtk.Template.Child()
    password_switch_row  = Gtk.Template.Child()
    password_view        = Gtk.Template.Child()
    username_entry       = Gtk.Template.Child()
    password_entry       = Gtk.Template.Child()
    password_back        = Gtk.Template.Child()
    password_signin      = Gtk.Template.Child()
    password_error       = Gtk.Template.Child()

    def __init__(self, app: JamjarApplication) -> None:
        super().__init__()
        self.app = app
        self._selected_server: Server | None = None
        self._quick_cancelled = False

        self.rescan_row.connect("activated", lambda *_: self._scan())
        self.manual_row.connect("activated", lambda *_: self.nav.push_by_tag("manual"))
        self.manual_continue.connect("clicked", self._on_manual_continue)
        self.quick_connect_copy.connect("clicked", self._on_copy_code)
        self.qc_retry.connect("clicked", lambda *_: self._start_quick_connect())
        self.password_switch_row.connect("activated", lambda *_: self._show_password())
        self.password_back.connect("clicked", lambda *_: self._show_quick_connect_and_retry())
        self.password_signin.connect("clicked", self._on_password_submit)
        self.connect("closed", lambda *_: self._on_dialog_closed())

        GLib.idle_add(self._scan)

    def _on_dialog_closed(self) -> None:
        self._quick_cancelled = True

    # ------- discovery -------

    def _scan(self) -> bool:
        self.discovery_status.set_title("Looking for Jellyfin servers…")
        self.discovered_group.set_visible(False)
        for child in list(self.discovered_group):
            if isinstance(child, Adw.ActionRow):
                self.discovered_group.remove(child)

        def done(future):
            try:
                servers = future.result()
            except Exception as e:
                log.warning("discovery failed: %s", e)
                servers = []
            GLib.idle_add(lambda: (self._show_servers(servers), False)[1])

        self.app.runner.submit(discover(timeout=3.0)).add_done_callback(done)
        return False

    def _show_servers(self, servers: list[Server]) -> None:
        if not servers:
            self.discovery_status.set_title("No servers found")
            self.discovery_status.set_description(
                "Make sure your server is on the same network, or connect manually below."
            )
            return
        self.discovery_status.set_title(f"Found {len(servers)} server"
                                         + ("s" if len(servers) > 1 else ""))
        self.discovery_status.set_description("")
        self.discovered_group.set_visible(True)
        for server in servers:
            row = Adw.ActionRow(title=escape_markup(server.name),
                                subtitle=escape_markup(server.address),
                                activatable=True)
            row.add_prefix(Gtk.Image.new_from_icon_name("network-server-symbolic"))
            row.add_suffix(Gtk.Image.new_from_icon_name("go-next-symbolic"))
            row.connect("activated", lambda _r, s=server: self._select_server(s))
            self.discovered_group.add(row)

    def _on_manual_continue(self, _btn) -> None:
        addr = self.address_entry.get_text().strip()
        if not addr:
            return
        if not addr.startswith(("http://", "https://")):
            addr = "http://" + addr
        server = Server(name=addr, address=addr, server_id="manual", source="manual")
        self._select_server(server)

    def _select_server(self, server: Server) -> None:
        self._selected_server = server
        self.nav.push_by_tag("auth")
        self._show_quick_connect()
        self._start_quick_connect()

    # ------- quick connect -------

    def _show_quick_connect(self) -> None:
        self.password_view.set_visible(False)
        self.quick_connect_view.set_visible(True)

    def _show_quick_connect_and_retry(self) -> None:
        self._show_quick_connect()
        self._start_quick_connect()

    def _show_password(self) -> None:
        self._quick_cancelled = True
        self.password_view.set_visible(True)
        self.quick_connect_view.set_visible(False)
        self.username_entry.grab_focus()

    def _show_qc_error(self, message: str) -> None:
        self.qc_error.set_label(message)
        self.qc_error.set_visible(True)
        self.qc_retry.set_visible(True)
        self.quick_connect_code.set_label("------")

    def _start_quick_connect(self) -> None:
        server = self._selected_server
        if server is None:
            return
        self._quick_cancelled = False
        device_id = self.app.settings.get_string("device-id")

        # Reset the visual state
        self.qc_error.set_visible(False)
        self.qc_retry.set_visible(False)
        self.quick_connect_code.set_label("------")
        log.info("starting Quick Connect against %s", server.address)

        async def run():
            async with Authenticator(server.address, device_id) as auth:
                if not await auth.quick_connect_enabled():
                    log.info("Quick Connect disabled on server")
                    return None
                return await auth.quick_connect(
                    on_code=lambda code: GLib.idle_add(
                        lambda: (self.quick_connect_code.set_label(code), False)[1]
                    ),
                    cancelled=lambda: self._quick_cancelled,
                )

        def done(future):
            try:
                result = future.result()
            except AuthError as e:
                log.info("quick connect ended: %s", e)
                if "cancelled" not in str(e).lower():
                    GLib.idle_add(lambda msg=str(e): (self._show_qc_error(msg), False)[1])
                return
            except Exception as e:
                log.warning("quick connect failed: %s", e)
                msg = f"Quick Connect failed: {e}"
                GLib.idle_add(lambda m=msg: (self._show_qc_error(m), False)[1])
                return
            if result is None:
                # QC disabled on server — drop the user straight onto the password form
                GLib.idle_add(lambda: (self._show_password(), False)[1])
                return
            self._on_authenticated(result, server)

        self.app.runner.submit(run()).add_done_callback(done)

    def _on_copy_code(self, _btn) -> None:
        clipboard = Gdk.Display.get_default().get_clipboard()
        clipboard.set(self.quick_connect_code.get_label())

    # ------- password -------

    def _on_password_submit(self, _btn) -> None:
        server = self._selected_server
        if server is None:
            return
        username = self.username_entry.get_text().strip()
        password = self.password_entry.get_text()
        if not username:
            return
        device_id = self.app.settings.get_string("device-id")
        self.password_signin.set_sensitive(False)
        self.password_error.set_visible(False)

        async def run():
            async with Authenticator(server.address, device_id) as auth:
                return await auth.login_password(username, password)

        def done(future):
            try:
                result = future.result()
            except AuthError as e:
                msg = str(e)
                GLib.idle_add(lambda: (self._show_error(msg), False)[1])
                return
            except Exception as e:
                log.warning("password login failed: %s", e)
                GLib.idle_add(lambda: (self._show_error("Login failed."), False)[1])
                return
            self._on_authenticated(result, server)

        self.app.runner.submit(run()).add_done_callback(done)

    def _show_error(self, message: str) -> None:
        self.password_signin.set_sensitive(True)
        self.password_error.set_label(message)
        self.password_error.set_visible(True)

    # ------- finalize -------

    def _on_authenticated(self, result, server: Server) -> None:
        self._quick_cancelled = True
        device_id = self.app.settings.get_string("device-id")

        async def build_client():
            client = JellyfinClient(server.address, result.user_id,
                                    result.access_token, device_id)
            await client.__aenter__()
            return client

        def done(future):
            try:
                client = future.result()
            except Exception as e:
                log.error("client setup failed: %s", e)
                return

            def apply():
                store_token(result.server_id or server.server_id,
                            result.user_id, result.access_token)
                self.app.settings.set_string("last-server-id",
                                             result.server_id or server.server_id)
                self.app.settings.set_string("last-user-id", result.user_id)
                self.app.settings.set_string("last-server-address", server.address)
                self.app.attach_session(client)
                self.emit("authenticated")
                self.close()
                return False

            GLib.idle_add(apply)

        self.app.runner.submit(build_client()).add_done_callback(done)
