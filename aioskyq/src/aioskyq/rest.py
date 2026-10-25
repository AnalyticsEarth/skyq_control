"""JSON REST client for port 9006 (`/as/...`)."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import aiohttp

from .const import API_ERROR_INVALID_URL, API_ERROR_NOT_SUPPORTED, REST_PORT
from .exceptions import (
    SkyQApiError,
    SkyQConnectionError,
    SkyQNotFoundError,
    SkyQNotSupportedError,
    SkyQTimeoutError,
)

_LOGGER = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 8.0


class RestClient:
    """Thin wrapper that maps the box's error documents onto exceptions."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        host: str,
        port: int = REST_PORT,
        timeout: float = DEFAULT_TIMEOUT,
        max_concurrency: int = 4,
    ) -> None:
        self._session = session
        self.host = host
        self.port = port
        self._timeout = timeout
        # The gateway's HTTP server has few workers; don't flood it.
        self._limiter = asyncio.Semaphore(max_concurrency)

    def url(self, path: str) -> str:
        return f"http://{self.host}:{self.port}/as/{path.lstrip('/')}"

    async def get(self, path: str, params: dict[str, Any] | None = None, *, timeout: float | None = None) -> Any:
        return await self.request("GET", path, params, timeout=timeout)

    async def post(self, path: str, params: dict[str, Any] | None = None, *, timeout: float | None = None) -> Any:
        return await self.request("POST", path, params, timeout=timeout)

    async def delete(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return await self.request("DELETE", path, params)

    async def request(
        self, method: str, path: str, params: dict[str, Any] | None = None, *, timeout: float | None = None
    ) -> Any:
        """Return the decoded JSON body, or None for an empty success."""
        url = self.url(path)
        client_timeout = aiohttp.ClientTimeout(total=timeout or self._timeout)
        _LOGGER.debug("%s %s %s", method, url, params or "")
        try:
            async with self._limiter, self._session.request(method, url, params=params, timeout=client_timeout) as resp:
                body = await resp.read()
                status = resp.status
        except TimeoutError as err:
            raise SkyQTimeoutError(f"{method} {url} timed out") from err
        except aiohttp.ClientError as err:
            raise SkyQConnectionError(f"{method} {url} failed: {err}") from err

        data: Any = None
        if body.strip():
            try:
                data = json.loads(body)
            except ValueError:
                data = body.decode("utf-8", "replace")

        if 200 <= status < 300 and not _is_error_document(data):
            return data
        raise _api_error(status, data)


def _is_error_document(data: Any) -> bool:
    return isinstance(data, dict) and "errorCode" in data and "userMessage" in data and len(data) <= 3


def _api_error(status: int, data: Any) -> SkyQApiError:
    code = message = developer = None
    if isinstance(data, dict):
        raw_code = data.get("errorCode")
        try:
            code = int(raw_code) if raw_code is not None else None
        except (TypeError, ValueError):
            code = None
        message, developer = data.get("userMessage"), data.get("developerMessage")
    if code == API_ERROR_NOT_SUPPORTED:
        return SkyQNotSupportedError(status, code, message, developer)
    if code == API_ERROR_INVALID_URL or status == 404:
        return SkyQNotFoundError(status, code, message, developer)
    return SkyQApiError(status, code, message, developer)
