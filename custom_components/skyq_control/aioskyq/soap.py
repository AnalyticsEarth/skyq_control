# Bundled copy of aioskyq; edit aioskyq/src/aioskyq and run scripts/sync_aioskyq.py.
"""UPnP/SOAP client for port 49153.

Sky Q only allows *reading* transport state over SOAP: Pause and SetAVTransportURI return
UPnP 705 "TRANSPORT IS LOCKED". Control goes through remote keys (see remote.py).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape

import aiohttp

from .const import (
    DESCRIPTION_INDEXES,
    SKY_PLAY_SERVICE,
    UPNP_ERROR_TRANSPORT_LOCKED,
    UPNP_PORT,
    UPNP_USER_AGENT,
)
from .exceptions import SkyQConnectionError, SkyQSoapError, SkyQTimeoutError, SkyQTransportLockedError
from .models import MediaInfo, PositionInfo, TransportInfo

_LOGGER = logging.getLogger(__name__)

SOAP_TIMEOUT = 5.0


@dataclass(frozen=True, slots=True)
class UpnpService:
    device_type: str
    service_type: str
    service_id: str
    control_url: str
    scpd_url: str | None
    description_index: int


@dataclass(frozen=True, slots=True)
class UpnpDevice:
    device_type: str
    friendly_name: str | None
    model_number: str | None
    description_index: int
    services: tuple[UpnpService, ...]


def _strip_ns(root: ET.Element) -> ET.Element:
    for element in root.iter():
        if isinstance(element.tag, str) and "}" in element.tag:
            element.tag = element.tag.split("}", 1)[1]
    return root


def _text(element: ET.Element | None, name: str) -> str | None:
    if element is None:
        return None
    child = element.find(name)
    return child.text.strip() if child is not None and child.text else None


def parse_description(xml: str, index: int) -> list[UpnpDevice]:
    """Parse one /descriptionN.xml document into its devices and services."""
    root = _strip_ns(ET.fromstring(xml))
    devices = []
    for dev in root.iter("device"):
        device_type = _text(dev, "deviceType") or ""
        services = []
        service_list = dev.find("serviceList")
        for svc in service_list.findall("service") if service_list is not None else ():
            stype, sid, control = _text(svc, "serviceType"), _text(svc, "serviceId"), _text(svc, "controlURL")
            if stype and sid and control:
                services.append(UpnpService(device_type, stype, sid, control, _text(svc, "SCPDURL"), index))
        devices.append(
            UpnpDevice(device_type, _text(dev, "friendlyName"), _text(dev, "modelNumber"), index, tuple(services))
        )
    return devices


def parse_soap_response(xml: str, action: str) -> dict[str, str | None]:
    """Return the out-arguments of `action`, or raise SkyQSoapError for a fault."""
    root = _strip_ns(ET.fromstring(xml))
    response = root.find(f".//{action}Response")
    if response is not None:
        return {child.tag: child.text for child in response}
    fault = root.find(".//Fault")
    if fault is None:
        return {}  # an empty envelope still means success (e.g. Play)
    error = root.find(".//UPnPError")
    code_text = _text(error, "errorCode")
    code = int(code_text) if code_text and code_text.isdigit() else None
    description = _text(error, "errorDescription") or _text(fault, "faultstring")
    if code == UPNP_ERROR_TRANSPORT_LOCKED:
        raise SkyQTransportLockedError(action, code, description)
    raise SkyQSoapError(action, code, description)


class SoapClient:
    def __init__(self, session: aiohttp.ClientSession, host: str, port: int = UPNP_PORT) -> None:
        self._session = session
        self.host = host
        self.port = port
        self.devices: list[UpnpDevice] = []
        self._play_service: UpnpService | None = None
        self._discover_lock = asyncio.Lock()

    def url(self, path: str) -> str:
        if path.startswith("http"):
            return path
        return f"http://{self.host}:{self.port}{path if path.startswith('/') else '/' + path}"

    async def discover(self) -> list[UpnpDevice]:
        """Fetch every description document the box serves."""

        async def fetch(index: int) -> list[UpnpDevice]:
            try:
                async with self._session.get(
                    self.url(f"/description{index}.xml"),
                    headers={"User-Agent": UPNP_USER_AGENT},
                    timeout=aiohttp.ClientTimeout(total=3),
                ) as resp:
                    if resp.status != 200:
                        return []
                    return parse_description(await resp.text(), index)
            except (aiohttp.ClientError, TimeoutError, ET.ParseError):
                return []

        results = await asyncio.gather(*(fetch(i) for i in DESCRIPTION_INDEXES))
        self.devices = [device for devices in results for device in devices]
        if not self.devices:
            raise SkyQConnectionError(f"No UPnP descriptions found on {self.host}:{self.port}")
        return self.devices

    async def play_service(self) -> UpnpService:
        """The SkyPlay service that answers GetTransportInfo.

        Gateways list SkyPlay twice; the copy on the SkyServe device rejects every action
        with "Invalid action", so the first one that works is kept.
        """
        if self._play_service:
            return self._play_service
        async with self._discover_lock:
            if self._play_service:
                return self._play_service
            if not self.devices:
                await self.discover()
            candidates = [s for d in self.devices for s in d.services if s.service_type == SKY_PLAY_SERVICE]
            # SkyControl devices first: that is where the working copy lives.
            candidates.sort(key=lambda s: "SkyControl" not in s.device_type)
            last_error: Exception | None = None
            for service in candidates:
                try:
                    await self.call(service, "GetTransportInfo", {"InstanceID": "0"})
                except SkyQSoapError as err:
                    last_error = err
                    continue
                self._play_service = service
                return service
            raise SkyQConnectionError(f"No working SkyPlay service on {self.host}") from last_error

    def reset(self) -> None:
        """Forget discovered services (e.g. after the box rebooted)."""
        self.devices = []
        self._play_service = None

    async def call(self, service: UpnpService, action: str, args: dict[str, str]) -> dict[str, str | None]:
        body = "".join(f"<{name}>{escape(value)}</{name}>" for name, value in args.items())
        envelope = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
            's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
            f'<s:Body><u:{action} xmlns:u="{service.service_type}">{body}</u:{action}></s:Body></s:Envelope>'
        )
        headers = {
            "Content-Type": 'text/xml; charset="utf-8"',
            "SOAPACTION": f'"{service.service_type}#{action}"',
            "User-Agent": UPNP_USER_AGENT,
        }
        try:
            async with self._session.post(
                self.url(service.control_url),
                data=envelope,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=SOAP_TIMEOUT),
            ) as resp:
                text = await resp.text()
        except TimeoutError as err:
            raise SkyQTimeoutError(f"SOAP {action} on {self.host} timed out") from err
        except aiohttp.ClientError as err:
            raise SkyQConnectionError(f"SOAP {action} on {self.host} failed: {err}") from err
        try:
            return parse_soap_response(text, action)
        except ET.ParseError as err:
            raise SkyQSoapError(action, None, f"unparseable response: {text[:120]!r}") from err

    async def call_play(self, action: str, **args: str) -> dict[str, str | None]:
        service = await self.play_service()
        try:
            return await self.call(service, action, {"InstanceID": "0", **args})
        except SkyQConnectionError:
            self.reset()  # the control URL can change after a reboot
            raise

    async def get_transport_info(self) -> TransportInfo:
        return TransportInfo.from_api(await self.call_play("GetTransportInfo"))

    async def get_media_info(self) -> MediaInfo:
        return MediaInfo.from_api(await self.call_play("GetMediaInfo"))

    async def get_position_info(self) -> PositionInfo:
        return PositionInfo.from_api(await self.call_play("GetPositionInfo"))

    async def get_transport_actions(self) -> list[str]:
        actions = (await self.call_play("GetCurrentTransportActions")).get("Actions") or ""
        return [a for a in actions.split(",") if a]
