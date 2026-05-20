"""Server discovery: UDP broadcast on port 7359, mDNS fallback."""

from __future__ import annotations

import asyncio
import json
import logging
import socket
from collections.abc import Callable, Iterable

from .models import Server

log = logging.getLogger(__name__)

JELLYFIN_UDP_PORT = 7359
DISCOVERY_PROBE = b"Who is JellyfinServer?"
MDNS_SERVICE = "_jellyfin._tcp.local."


class _DiscoveryProtocol(asyncio.DatagramProtocol):
    def __init__(self, on_found: Callable[[Server], None]) -> None:
        self.on_found = on_found

    def datagram_received(self, data: bytes, addr) -> None:  # type: ignore[override]
        try:
            payload = json.loads(data.decode("utf-8", errors="replace"))
        except ValueError:
            log.debug("ignoring non-JSON discovery datagram from %s", addr)
            return

        try:
            self.on_found(Server(
                name=payload["Name"],
                address=payload["Address"],
                server_id=payload["Id"],
                source="udp",
            ))
        except KeyError as e:
            log.debug("missing field %s in discovery payload from %s", e, addr)


async def discover_udp(timeout: float = 2.5) -> list[Server]:
    """Broadcast a Jellyfin discovery probe and collect responses."""
    found_ids: set[str] = set()
    results: list[Server] = []

    def collect(srv: Server) -> None:
        if srv.server_id in found_ids:
            return
        found_ids.add(srv.server_id)
        results.append(srv)

    loop = asyncio.get_running_loop()
    transport, _ = await loop.create_datagram_endpoint(
        lambda: _DiscoveryProtocol(collect),
        local_addr=("0.0.0.0", 0),
        allow_broadcast=True,
    )
    sock = transport.get_extra_info("socket")
    if sock is not None:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)

    try:
        transport.sendto(DISCOVERY_PROBE, ("255.255.255.255", JELLYFIN_UDP_PORT))
        await asyncio.sleep(timeout)
    finally:
        transport.close()

    return results


async def discover_mdns(timeout: float = 3.0) -> list[Server]:
    """Browse for `_jellyfin._tcp` records via Avahi/zeroconf."""
    try:
        from zeroconf.asyncio import AsyncServiceBrowser, AsyncZeroconf
    except ImportError:
        log.info("python-zeroconf not available; skipping mDNS discovery")
        return []

    found: dict[str, Server] = {}

    async with AsyncZeroconf() as azc:
        zc = azc.zeroconf

        def on_change(zeroconf, service_type, name, state_change):
            from zeroconf import ServiceStateChange
            if state_change is not ServiceStateChange.Added:
                return
            info = zeroconf.get_service_info(service_type, name, timeout=1500)
            if not info or not info.addresses:
                return
            addr = socket.inet_ntoa(info.addresses[0])
            base = f"http://{addr}:{info.port}"
            sid = (info.properties or {}).get(b"id", b"").decode() or name
            found[sid] = Server(
                name=name.split(".")[0],
                address=base,
                server_id=sid,
                source="mdns",
            )

        AsyncServiceBrowser(zc, MDNS_SERVICE, handlers=[on_change])
        await asyncio.sleep(timeout)

    return list(found.values())


async def discover(timeout: float = 3.0) -> list[Server]:
    """Run UDP and mDNS in parallel; merge by server_id."""
    udp, mdns = await asyncio.gather(
        discover_udp(timeout),
        discover_mdns(timeout),
        return_exceptions=True,
    )

    merged: dict[str, Server] = {}
    for batch in (udp, mdns):
        if isinstance(batch, Exception):
            log.warning("discovery branch failed: %s", batch)
            continue
        for srv in batch:
            merged.setdefault(srv.server_id, srv)
    return list(merged.values())


def merge_results(*batches: Iterable[Server]) -> list[Server]:
    seen: dict[str, Server] = {}
    for batch in batches:
        for srv in batch:
            seen.setdefault(srv.server_id, srv)
    return list(seen.values())
