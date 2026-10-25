# Bundled copy of aioskyq; edit aioskyq/src/aioskyq and run scripts/sync_aioskyq.py.
"""Remote-control keys over TCP 49160 (5900 on firmware older than 060)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Iterable

from .const import KEY_ALIASES, LEGACY_REMOTE_PORT, REMOTE_PORT, Key
from .exceptions import SkyQConnectionError, SkyQTimeoutError

_LOGGER = logging.getLogger(__name__)

HANDSHAKE_TIMEOUT = 5.0
# The box drops presses that arrive too quickly.
DEFAULT_KEY_GAP = 0.5

KeyLike = Key | str | int


def resolve_key(key: KeyLike) -> Key:
    """Accept a Key, a code, or a name ("channelup", "CHANNEL_UP", "5")."""
    if isinstance(key, Key):
        return key
    if isinstance(key, int):
        return Key(key)
    name = key.strip()
    if name.lower() in KEY_ALIASES:
        return KEY_ALIASES[name.lower()]
    try:
        return Key[name.upper()]
    except KeyError:
        raise ValueError(f"Unknown Sky Q key: {key!r}") from None


def key_packet(code: int) -> bytearray:
    return bytearray([4, 1, 0, 0, 0, 0, 224 + code // 16, code % 16])


class RemoteClient:
    def __init__(
        self, host: str, port: int = REMOTE_PORT, *, key_gap: float = DEFAULT_KEY_GAP, allow_legacy_port: bool = True
    ) -> None:
        self.host = host
        self.port = port
        self.key_gap = key_gap
        self._allow_legacy = allow_legacy_port and port == REMOTE_PORT
        self._lock = asyncio.Lock()

    async def press(self, *keys: KeyLike | Iterable[KeyLike]) -> None:
        """Press keys in order, `key_gap` seconds apart. Presses from callers never interleave."""
        flat: list[Key] = []
        for key in keys:
            if isinstance(key, Key | str | int):
                flat.append(resolve_key(key))
            else:
                flat.extend(resolve_key(k) for k in key)
        async with self._lock:
            for index, key in enumerate(flat):
                if index:
                    await asyncio.sleep(self.key_gap)
                await self._send(key)

    async def check(self) -> bool:
        """True if the port answers with the `SKY` banner (no key is sent)."""
        try:
            reader, writer = await asyncio.wait_for(asyncio.open_connection(self.host, self.port), 3)
        except (OSError, TimeoutError):
            return False
        try:
            banner = await asyncio.wait_for(reader.read(16), 3)
        except TimeoutError:
            banner = b""
        finally:
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()
        return banner.startswith(b"SKY")

    async def _send(self, key: Key) -> None:
        try:
            await self._send_on(self.port, key)
        except SkyQConnectionError:
            if not self._allow_legacy:
                raise
            _LOGGER.debug("Port %s refused; trying legacy port %s", self.port, LEGACY_REMOTE_PORT)
            await self._send_on(LEGACY_REMOTE_PORT, key)
            self.port = LEGACY_REMOTE_PORT
            self._allow_legacy = False

    async def _send_on(self, port: int, key: Key) -> None:
        _LOGGER.debug("Key %s (%d) -> %s:%d", key.name, key.value, self.host, port)
        try:
            reader, writer = await asyncio.wait_for(asyncio.open_connection(self.host, port), 3)
        except (OSError, TimeoutError) as err:
            raise SkyQConnectionError(f"Remote port {self.host}:{port} unreachable: {err}") from err
        packet = key_packet(key.value)
        try:
            async with asyncio.timeout(HANDSHAKE_TIMEOUT):
                # Echo the box's handshake (12 bytes, then 1 byte at a time) until it sends
                # its 24-byte ready block, then send key-down and key-up.
                echo_len = 12
                while True:
                    data = await reader.read(1024)
                    if not data:
                        raise SkyQConnectionError(f"{self.host}:{port} closed during handshake")
                    if len(data) < 24:
                        writer.write(data[:echo_len])
                        echo_len = 1
                        await writer.drain()
                        continue
                    writer.write(bytes(packet))
                    packet[1] = 0
                    writer.write(bytes(packet))
                    await writer.drain()
                    return
        except TimeoutError as err:
            raise SkyQTimeoutError(f"Remote handshake with {self.host}:{port} timed out") from err
        except OSError as err:
            raise SkyQConnectionError(f"Remote handshake with {self.host}:{port} failed: {err}") from err
        finally:
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()
