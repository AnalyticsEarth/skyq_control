"""Exceptions raised by aioskyq."""

from __future__ import annotations


class SkyQError(Exception):
    """Base class for all aioskyq errors."""


class SkyQConnectionError(SkyQError):
    """The box could not be reached (off, wrong IP, network down)."""


class SkyQTimeoutError(SkyQConnectionError):
    """The box accepted the connection but did not answer in time."""


class SkyQApiError(SkyQError):
    """The REST API returned an error document: {errorCode, userMessage, developerMessage}."""

    def __init__(
        self, status: int, code: int | None = None, message: str | None = None, developer_message: str | None = None
    ) -> None:
        self.status = status
        self.code = code
        self.message = message
        self.developer_message = developer_message
        super().__init__(f"HTTP {status} error {code}: {message or developer_message or 'no detail'}")


class SkyQNotFoundError(SkyQApiError):
    """Unknown URL (errorCode 101 / HTTP 404) or item."""


class SkyQNotSupportedError(SkyQApiError):
    """The endpoint exists but this device does not implement it (errorCode 104)."""


class SkyQSoapError(SkyQError):
    """A UPnP/SOAP action failed."""

    def __init__(self, action: str, code: int | None, description: str | None) -> None:
        self.action = action
        self.code = code
        self.description = description
        super().__init__(f"{action} failed: UPnP error {code} {description or ''}".strip())


class SkyQTransportLockedError(SkyQSoapError):
    """UPnP 705 TRANSPORT IS LOCKED: Sky Q refuses SOAP transport control; use remote keys."""


class SkyQUnsupportedDeviceError(SkyQError):
    """The device is not a Sky Q box this library can control (IP set-top box, Sky Glass TV)."""
