"""SSDP discovery of Sky Q gateways and Mini boxes."""

from __future__ import annotations

import asyncio
import socket
from dataclasses import dataclass

from .const import SSDP_TARGETS

SSDP_ADDR = ("239.255.255.250", 1900)


@dataclass(frozen=True, slots=True)
class DiscoveredBox:
    host: str
    search_target: str
    location: str | None
    server: str | None

    @property
    def is_gateway(self) -> bool:
        return "GatewaySkyControl" in self.search_target


class _SsdpProtocol(asyncio.DatagramProtocol):
    def __init__(self) -> None:
        self.found: dict[str, DiscoveredBox] = {}

    def datagram_received(self, data: bytes, addr: tuple[str | object, int]) -> None:
        headers = {}
        for line in data.decode("latin-1").split("\r\n")[1:]:
            name, sep, value = line.partition(":")
            if sep:
                headers[name.strip().upper()] = value.strip()
        target = headers.get("ST", "")
        # Other UPnP devices (e.g. Hue bridges) answer every M-SEARCH; keep Sky Q only.
        if "SkyControl" not in target:
            return
        host = str(addr[0])
        if host not in self.found or "Gateway" in target:
            self.found[host] = DiscoveredBox(host, target, headers.get("LOCATION"), headers.get("SERVER"))


async def discover(timeout: float = 4.0) -> list[DiscoveredBox]:
    """Return every Sky Q box that answers within `timeout` seconds (gateways first)."""
    loop = asyncio.get_running_loop()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
    sock.bind(("", 0))
    transport, protocol = await loop.create_datagram_endpoint(_SsdpProtocol, sock=sock)
    try:
        for target in SSDP_TARGETS:
            message = (
                "M-SEARCH * HTTP/1.1\r\n"
                f"HOST: {SSDP_ADDR[0]}:{SSDP_ADDR[1]}\r\n"
                'MAN: "ssdp:discover"\r\nMX: 2\r\n'
                f"ST: {target}\r\n\r\n"
            )
            transport.sendto(message.encode(), SSDP_ADDR)
        await asyncio.sleep(timeout)
    finally:
        transport.close()
    return sorted(protocol.found.values(), key=lambda box: (not box.is_gateway, box.host))
