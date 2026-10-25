"""Fixtures for the Sky Q Control integration tests.

SkyQBox is mocked, but every model is built from the real (sanitized) payloads in
aioskyq/tests/fixtures, so the integration sees exactly what real boxes send.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Generator
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.skyq_control.aioskyq import (
    App,
    AppsStatus,
    Channel,
    DeviceInfo,
    EpgEvent,
    Favourite,
    Genre,
    MediaInfo,
    PinStatus,
    PositionInfo,
    PowerState,
    PvrStatus,
    Recording,
    RecordingList,
    Storage,
    SystemStatus,
    TransportInfo,
)
from custom_components.skyq_control.aioskyq.soap import UpnpDevice
from custom_components.skyq_control.const import DOMAIN

FIXTURES = Path(__file__).parents[1] / "aioskyq" / "tests" / "fixtures"

GATEWAY_HOST = "192.0.2.10"
MINI_HOST = "192.0.2.11"
GATEWAY_SERIAL = "GW0000001"
MINI_SERIAL = "MINI000001"


def load(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text())


def device_info(kind: str, host: str, serial: str) -> DeviceInfo:
    system = {**load(f"{kind}/rest/system_information.json"), "MACAddress": f"00:11:22:33:44:{len(serial):02x}"}
    if kind == "mini":
        system["gatewayIPAddress"] = GATEWAY_HOST
    info = DeviceInfo.from_api(host, load(f"{kind}/rest/system_deviceinformation.json"), system)
    return replace(info, serial_number=serial, mac_address=system["MACAddress"])


def programme(offset_minutes: int = 0, title: str = "Kingdom", sid: int = 3008) -> EpgEvent:
    """A 30-minute programme; offset 0 started ten minutes ago."""
    start = datetime.now(UTC).replace(second=0, microsecond=0) + timedelta(minutes=offset_minutes - 10)
    return EpgEvent.from_api(
        sid,
        {
            "eid": f"E{sid:x}-{offset_minutes + 100:x}",
            "t": title,
            "st": int(start.timestamp()),
            "d": 1800,
            "programmeuuid": "8649a421-a57d-4151-b9df-b4ad0662b3f3",
        },
    )


def day_of_programmes(sid: int) -> list[EpgEvent]:
    """Kingdom (on now), News, then Show 2, Show 3, ... every half hour for a day."""
    titles = {0: "Kingdom", 30: "News"}
    return [programme(offset, titles.get(offset, f"Show {offset // 30}"), sid) for offset in range(0, 24 * 60, 30)]


def make_guide() -> MagicMock:
    """Stands in for the shared EpgClient (one per territory, shared by every box)."""

    def schedule(channels: Any, start: datetime, end: datetime) -> dict[int, list[EpgEvent]]:
        sids = [c if isinstance(c, int) else c.sid for c in channels]
        return {sid: [e for e in day_of_programmes(sid) if e.end > start and e.start < end] for sid in sids}

    guide = MagicMock(name="EpgClient")
    guide.now_and_next = AsyncMock(side_effect=lambda *_: (programme(0), programme(30, "News")))
    guide.get_schedule = AsyncMock(side_effect=schedule)
    guide.get_events = AsyncMock(
        side_effect=lambda channel, start, end: schedule([channel], start, end)[getattr(channel, "sid", channel)]
    )
    return guide


@dataclass
class FakeBox:
    """State behind one mocked SkyQBox."""

    kind: str
    host: str
    serial: str
    guide: MagicMock = field(default_factory=make_guide)
    power: PowerState = PowerState.ON
    uri: str | None = "xsi://BC0"
    mock: MagicMock = field(init=False)
    listeners: dict[str, Callable[[Any], None]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        box = MagicMock(name=f"SkyQBox({self.host})")
        box.host = self.host
        box.get_device_info = AsyncMock(side_effect=lambda **_: device_info(self.kind, self.host, self.serial))
        box.get_power_state = AsyncMock(side_effect=lambda: self.power)
        box.get_channels = AsyncMock(
            return_value=[Channel.from_api(s) for s in load("rest/services_bouquet.json")["services"]]
        )
        box.get_favourites = AsyncMock(
            return_value=[
                Favourite.from_api(f) for f in load(f"{self.kind}/rest/services_favourites.json")["favourites"]
            ]
        )
        box.get_genres = AsyncMock(
            return_value=[Genre.from_api(g) for g in load("rest/services_genres.json")["genres"]]
        )
        box.get_apps = AsyncMock(return_value=[App.from_api(a) for a in load("rest/apps.json")["apps"]])
        box.get_recordings = AsyncMock(return_value=RecordingList.from_api(load("rest/pvr_list.json")))
        box.get_recording = AsyncMock(
            side_effect=lambda pvrid: Recording.from_api(
                next(i for i in load("rest/pvr_list.json")["pvrItems"] if i["pvrid"] == pvrid)
            )
        )
        box.get_storage = AsyncMock(return_value=Storage.from_api(load(f"{self.kind}/rest/pvr_storage.json")))
        box.get_pin_status = AsyncMock(return_value=PinStatus.from_api(load(f"{self.kind}/rest/pin.json")))
        box.get_transport_info = AsyncMock(
            side_effect=lambda: TransportInfo.from_api(
                {"CurrentTransportState": "PLAYING" if self.uri else "NO_MEDIA_PRESENT", "CurrentSpeed": "1"}
            )
        )
        box.get_media_info = AsyncMock(side_effect=lambda: MediaInfo.from_api({"CurrentURI": self.uri}))
        box.get_position_info = AsyncMock(return_value=PositionInfo.from_api({"RelTime": "0:10:00"}))
        box.epg = self.guide
        box.territory = "GB"
        box.book_recording = AsyncMock(return_value=None)
        box.close = AsyncMock()
        for command in (
            "press",
            "change_channel",
            "tune",
            "launch_app",
            "turn_on",
            "turn_off",
            "play",
            "pause",
            "stop",
        ):
            setattr(box, command, AsyncMock())
        box.streams_connected = {"system/status": True}
        box.soap.devices = [UpnpDevice("urn:schemas-nds-com:device:GatewaySkyControl:2", "000000000", "Q370", 1, ())]
        for name in ("system_status", "apps", "pvr", "channel_list"):
            box.attach_mock(MagicMock(side_effect=self._subscriber(name)), f"subscribe_{name}")
        self.mock = box

    def _subscriber(self, name: str) -> Callable[[Callable[[Any], None]], MagicMock]:
        def subscribe(listener: Callable[[Any], None]) -> MagicMock:
            self.listeners[name] = listener
            return MagicMock(name=f"unsubscribe_{name}")

        return subscribe

    # Push helpers, as aioskyq would call the listeners.
    def push_system_status(self, state: str = "active") -> None:
        self.listeners["system_status"](
            SystemStatus.from_api({**load(f"{self.kind}/ws/system_status.json"), "systemstate": {"state": state}})
        )

    def push_app(self, app_id: str | None) -> None:
        apps = [{"appId": app_id, "status": "VISIBLE"}] if app_id else []
        self.listeners["apps"](AppsStatus.from_api({"apps": apps}))

    def push_pvr(self) -> None:
        self.listeners["pvr"](PvrStatus.from_api(load(f"{self.kind}/ws/pvr_status.json")))

    def push_channel_list(self) -> None:
        self.listeners["channel_list"]({"documentId": "2"})


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Load custom_components/ in every test."""


@pytest.fixture
def guide() -> MagicMock:
    return make_guide()


@pytest.fixture
def boxes(guide: MagicMock) -> dict[str, FakeBox]:
    return {
        GATEWAY_HOST: FakeBox("gateway", GATEWAY_HOST, GATEWAY_SERIAL, guide),
        MINI_HOST: FakeBox("mini", MINI_HOST, MINI_SERIAL, guide),
    }


@pytest.fixture
def mock_skyq(boxes: dict[str, FakeBox], guide: MagicMock) -> Generator[MagicMock]:
    """Patch SkyQBox everywhere; unknown hosts behave like a box that is off."""
    from custom_components.skyq_control.aioskyq import SkyQConnectionError

    def factory(host: str, *args: Any, **kwargs: Any) -> MagicMock:
        if host in boxes:
            return boxes[host].mock
        dead = MagicMock()
        dead.get_device_info = AsyncMock(side_effect=SkyQConnectionError("unreachable"))
        dead.close = AsyncMock()
        return dead

    with (
        patch("custom_components.skyq_control.SkyQBox", side_effect=factory) as cls,
        patch("custom_components.skyq_control.config_flow.SkyQBox", side_effect=factory),
        patch("custom_components.skyq_control.EpgClient", return_value=guide),
    ):
        yield cls


def make_entry(host: str = GATEWAY_HOST, serial: str = GATEWAY_SERIAL, **kwargs: Any) -> MockConfigEntry:
    title = "Sky Q Gateway" if host == GATEWAY_HOST else f"Sky Q Mini ({host})"
    kwargs.setdefault("minor_version", 2)
    return MockConfigEntry(domain=DOMAIN, unique_id=serial, title=title, data={CONF_HOST: host}, **kwargs)


async def setup_entry(hass: HomeAssistant, entry: MockConfigEntry) -> MockConfigEntry:
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry
