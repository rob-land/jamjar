"""Jellyfin authentication: Quick Connect + username/password."""

from __future__ import annotations

import asyncio
import logging
import socket
import uuid
from typing import Awaitable, Callable, Optional

import aiohttp

from . import __version__
from .models import AuthResult

log = logging.getLogger(__name__)


def device_name() -> str:
    try:
        raw = socket.gethostname() or "Linux"
    except Exception:
        return "Linux"
    # Strip characters that would break the MediaBrowser header parser
    # (quotes, commas, equals). Keep it short to stay within reasonable
    # header limits.
    cleaned = "".join(c for c in raw if c.isalnum() or c in "-_.").strip()
    return cleaned[:64] or "Linux"


def new_device_id() -> str:
    return str(uuid.uuid4())


def auth_header(device_id: str, token: str = "") -> dict[str, str]:
    """Build the MediaBrowser Authorization header value.

    Jellyfin also accepts the same value under `X-Emby-Authorization`; we send
    both for compatibility with older deployments and reverse-proxy setups
    that strip non-standard `Authorization` schemes.
    """
    parts = [
        f'Client="Jamjar"',
        f'Device="{device_name()}"',
        f'DeviceId="{device_id}"',
        f'Version="{__version__}"',
        f'Token="{token}"',
    ]
    value = "MediaBrowser " + ", ".join(parts)
    return {
        "Authorization":         value,
        "X-Emby-Authorization":  value,
    }


class AuthError(Exception):
    """Raised when authentication fails."""


class Authenticator:
    """Lightweight authenticator that doesn't depend on the full client."""

    def __init__(self, base_url: str, device_id: str,
                 session: Optional[aiohttp.ClientSession] = None):
        self.base = base_url.rstrip("/")
        self.device_id = device_id
        self._session = session
        self._owns_session = session is None

    async def __aenter__(self) -> "Authenticator":
        if self._session is None:
            self._session = aiohttp.ClientSession()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._owns_session and self._session is not None:
            await self._session.close()

    @property
    def session(self) -> aiohttp.ClientSession:
        if self._session is None:
            raise RuntimeError("Authenticator used outside `async with`")
        return self._session

    async def login_password(self, username: str, password: str) -> AuthResult:
        headers = auth_header(self.device_id, "")
        async with self.session.post(
            f"{self.base}/Users/AuthenticateByName",
            headers=headers,
            json={"Username": username, "Pw": password},
        ) as r:
            if r.status == 401:
                raise AuthError("Invalid username or password")
            r.raise_for_status()
            data = await r.json()
        return AuthResult(
            access_token=data["AccessToken"],
            user_id=data["User"]["Id"],
            server_id=data.get("ServerId", ""),
            server_address=self.base,
            username=username,
        )

    async def quick_connect(
        self,
        on_code: Callable[[str], None],
        cancelled: Optional[Callable[[], bool]] = None,
        poll_interval: float = 3.0,
    ) -> AuthResult:
        headers = auth_header(self.device_id, "")

        # POST with an explicit empty JSON body. Some Jellyfin reverse-proxy
        # setups reject zero-length POSTs; sending `{}` is harmless to the
        # server and dodges that whole class of breakage.
        url = f"{self.base}/QuickConnect/Initiate"
        log.info("Quick Connect: POST %s", url)
        try:
            async with self.session.post(url, headers=headers, json={}) as r:
                body = await r.text()
                log.debug("Quick Connect Initiate -> %s %s", r.status, body[:512])
                if r.status >= 400:
                    raise AuthError(
                        f"Server rejected Quick Connect Initiate (HTTP {r.status}). "
                        f"Is Quick Connect enabled in the dashboard?"
                    )
                try:
                    import json as _json
                    init = _json.loads(body)
                except ValueError as e:
                    raise AuthError(f"Unexpected Quick Connect response: {e}") from e
        except aiohttp.ClientError as e:
            raise AuthError(f"Could not reach the server: {e}") from e

        secret = init.get("Secret")
        code = init.get("Code")
        if not secret or not code:
            raise AuthError(
                "Server returned a Quick Connect response without a Code. "
                f"Got fields: {sorted(init.keys())}"
            )
        log.info("Quick Connect: server issued code %s", code)
        on_code(code)

        while True:
            await asyncio.sleep(poll_interval)
            if cancelled and cancelled():
                raise AuthError("Quick Connect cancelled")

            try:
                async with self.session.get(
                    f"{self.base}/QuickConnect/Connect",
                    params={"Secret": secret},
                    headers=headers,
                ) as r:
                    if r.status == 404:
                        raise AuthError(
                            "Server forgot this Quick Connect session "
                            "(it may have expired). Try again."
                        )
                    r.raise_for_status()
                    state = await r.json()
                    if state.get("Authenticated"):
                        log.info("Quick Connect: code %s authenticated", code)
                        break
            except aiohttp.ClientError as e:
                raise AuthError(f"Lost connection while polling: {e}") from e

        async with self.session.post(
            f"{self.base}/Users/AuthenticateWithQuickConnect",
            headers=headers,
            json={"Secret": secret},
        ) as r:
            r.raise_for_status()
            data = await r.json()

        return AuthResult(
            access_token=data["AccessToken"],
            user_id=data["User"]["Id"],
            server_id=data.get("ServerId", ""),
            server_address=self.base,
            username=data["User"].get("Name", ""),
        )

    async def quick_connect_enabled(self) -> bool:
        url = f"{self.base}/QuickConnect/Enabled"
        try:
            async with self.session.get(
                url, headers=auth_header(self.device_id, ""),
            ) as r:
                body = await r.text()
                log.debug("Quick Connect Enabled -> %s %s", r.status, body[:64])
                if r.status != 200:
                    return False
                return body.strip().lower() == "true"
        except aiohttp.ClientError as e:
            log.info("Quick Connect Enabled probe failed: %s", e)
            return False


async def login_password(base_url: str, device_id: str,
                         username: str, password: str) -> AuthResult:
    async with Authenticator(base_url, device_id) as auth:
        return await auth.login_password(username, password)


async def quick_connect(base_url: str, device_id: str,
                        on_code: Callable[[str], None]) -> AuthResult:
    async with Authenticator(base_url, device_id) as auth:
        return await auth.quick_connect(on_code)
