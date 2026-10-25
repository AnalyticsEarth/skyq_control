# Bundled copy of aioskyq; edit aioskyq/src/aioskyq and run scripts/sync_aioskyq.py.
"""Persistent `/as/.../status` websockets.

The box sends a full snapshot on connect and then pushes changes. The gateway sometimes
stalls a socket silently, so streams reconnect on a fixed interval as well as on errors;
every reconnect delivers a fresh snapshot, so consumers can treat each message as state.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

import aiohttp

from .const import REST_PORT

_LOGGER = logging.getLogger(__name__)

MessageCallback = Callable[[dict[str, Any]], Awaitable[None] | None]
ConnectionCallback = Callable[[bool], Awaitable[None] | None]

DEFAULT_REFRESH_INTERVAL = 600.0
MAX_BACKOFF = 60.0
CONNECT_TIMEOUT = 10.0
CLOSE_TIMEOUT = 3.0


async def _maybe_await(result: Awaitable[None] | None) -> None:
    if inspect.isawaitable(result):
        await result


class StatusStream:
    def __init__(
        self,
        session: aiohttp.ClientSession,
        host: str,
        path: str,
        on_message: MessageCallback,
        *,
        port: int = REST_PORT,
        refresh_interval: float = DEFAULT_REFRESH_INTERVAL,
        on_connection_change: ConnectionCallback | None = None,
    ) -> None:
        self._session = session
        self.host = host
        self.path = path
        self.port = port
        self._on_message = on_message
        self._on_connection_change = on_connection_change
        self._refresh_interval = refresh_interval
        self._task: asyncio.Task[None] | None = None
        self._connected = False
        self.last_message: dict[str, Any] | None = None

    @property
    def url(self) -> str:
        return f"ws://{self.host}:{self.port}/as/{self.path}"

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        if not self.running:
            self._task = asyncio.create_task(self._run(), name=f"aioskyq-ws-{self.host}-{self.path}")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await self._set_connected(False)

    async def _set_connected(self, value: bool) -> None:
        if value != self._connected:
            self._connected = value
            if self._on_connection_change:
                await _maybe_await(self._on_connection_change(value))

    async def _run(self) -> None:
        backoff = 1.0
        while True:
            try:
                ws = await asyncio.wait_for(self._session.ws_connect(self.url), CONNECT_TIMEOUT)
            except (aiohttp.ClientError, TimeoutError) as err:
                _LOGGER.debug("Websocket %s unavailable (%s); retrying in %.0fs", self.url, err, backoff)
                await self._set_connected(False)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, MAX_BACKOFF)
                continue
            await self._set_connected(True)
            backoff = 1.0
            try:
                clean = await self._receive_until_refresh(ws)
            finally:
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(ws.close(), CLOSE_TIMEOUT)
            if not clean:
                await self._set_connected(False)
                await asyncio.sleep(backoff)

    async def _receive_until_refresh(self, ws: aiohttp.ClientWebSocketResponse) -> bool:
        """Dispatch messages until the refresh interval elapses (True) or the socket fails (False)."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._refresh_interval
        while (remaining := deadline - loop.time()) > 0:
            try:
                # Outer guard in case the box stops reading and aiohttp's own timeout never fires.
                async with asyncio.timeout(remaining + 5):
                    msg = await ws.receive(timeout=remaining)
            except TimeoutError:
                return True
            if msg.type is aiohttp.WSMsgType.TEXT:
                await self._dispatch(msg.data)
            elif msg.type is aiohttp.WSMsgType.BINARY:
                await self._dispatch(msg.data.decode("utf-8", "replace"))
            elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                _LOGGER.debug("Websocket %s closed by box", self.url)
                return False
        return True

    async def _dispatch(self, text: str) -> None:
        try:
            data = json.loads(text)
        except ValueError:
            _LOGGER.debug("Ignoring non-JSON message on %s: %.80s", self.url, text)
            return
        if not isinstance(data, dict):
            return
        self.last_message = data
        try:
            await _maybe_await(self._on_message(data))
        except Exception:
            _LOGGER.exception("Error in %s websocket callback", self.path)
