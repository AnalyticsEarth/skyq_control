"""Entry setup, device registry, coordinator behaviour and diagnostics."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed
from pytest_homeassistant_custom_component.components.diagnostics import get_diagnostics_for_config_entry
from pytest_homeassistant_custom_component.typing import ClientSessionGenerator

from custom_components.skyq_control.aioskyq import PowerState, SkyQConnectionError
from custom_components.skyq_control.const import DOMAIN

from .conftest import GATEWAY_HOST, GATEWAY_SERIAL, MINI_HOST, MINI_SERIAL, FakeBox, make_entry
from .conftest import setup_entry as _setup_entry

pytestmark = pytest.mark.usefixtures("mock_skyq")


async def setup_entry(hass: HomeAssistant, entry: MockConfigEntry) -> MockConfigEntry:
    """Set up an entry with a listener attached: coordinators only poll while entities listen,
    and this skeleton has no platforms yet."""
    entry = await _setup_entry(hass, entry)
    if entry.state is ConfigEntryState.LOADED:
        entry.async_on_unload(entry.runtime_data.async_add_listener(lambda: None))
    return entry


async def test_setup_and_unload(hass: HomeAssistant, boxes: dict[str, FakeBox]) -> None:
    entry = await setup_entry(hass, make_entry())
    assert entry.state is ConfigEntryState.LOADED
    data = entry.runtime_data.data
    assert data.device.is_gateway and data.power is PowerState.ON
    assert data.channel and data.channel.number == "101"
    assert data.programme and data.programme.title == "Kingdom"
    assert data.next_programme and data.next_programme.title == "News"
    assert len(data.channels) == 377 and data.favourites and "YouTube" in data.apps
    assert data.recordings and len(data.recordings.recordings) == 7 and data.storage and data.pin
    assert set(boxes[GATEWAY_HOST].listeners) == {"system_status", "apps", "pvr", "channel_list"}

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED
    boxes[GATEWAY_HOST].mock.close.assert_awaited()


async def test_device_registry(hass: HomeAssistant, device_registry: dr.DeviceRegistry) -> None:
    entry = await setup_entry(hass, make_entry())
    device = device_registry.async_get_device_by_identifier((DOMAIN, GATEWAY_SERIAL), entry.entry_id)
    assert device is not None
    assert device.config_entry_id == entry.entry_id
    assert (device.manufacturer, device.model, device.model_id) == ("Sky", "Sky Q Gateway", "ES240")
    assert device.sw_version == "Q370.000.09.00-AS_37.2.1" and device.hw_version == "Falcon"
    assert device.serial_number == GATEWAY_SERIAL
    assert (dr.CONNECTION_NETWORK_MAC, "00:11:22:33:44:09") in device.connections


@pytest.mark.parametrize("gateway_first", [True, False])
async def test_mini_linked_to_gateway(
    hass: HomeAssistant, device_registry: dr.DeviceRegistry, gateway_first: bool
) -> None:
    gateway_entry, mini_entry = make_entry(), make_entry(MINI_HOST, MINI_SERIAL)
    for entry in [gateway_entry, mini_entry] if gateway_first else [mini_entry, gateway_entry]:
        await setup_entry(hass, entry)
    gateway = device_registry.async_get_device_by_identifier((DOMAIN, GATEWAY_SERIAL), gateway_entry.entry_id)
    mini = device_registry.async_get_device_by_identifier((DOMAIN, MINI_SERIAL), mini_entry.entry_id)
    assert gateway and mini and mini.via_device_id == gateway.id
    assert mini.model == "Sky Q Mini" and mini.model_id == "EM150"


async def test_pvr_fetched_once_per_household(hass: HomeAssistant, boxes: dict[str, FakeBox]) -> None:
    mini_alone = await setup_entry(hass, make_entry(MINI_HOST, MINI_SERIAL))
    assert mini_alone.runtime_data.fetches_pvr  # no gateway configured: the Mini fetches recordings
    await setup_entry(hass, make_entry())
    assert not mini_alone.runtime_data.fetches_pvr  # now the gateway does

    boxes[MINI_HOST].mock.get_recordings.reset_mock()
    await hass.config_entries.async_reload(mini_alone.entry_id)
    await hass.async_block_till_done()
    boxes[MINI_HOST].mock.get_recordings.assert_not_awaited()


async def test_setup_retry_when_unreachable(hass: HomeAssistant, boxes: dict[str, FakeBox]) -> None:
    boxes[GATEWAY_HOST].power = PowerState.OFF
    entry = await setup_entry(hass, make_entry())
    assert entry.state is ConfigEntryState.SETUP_RETRY
    boxes[GATEWAY_HOST].mock.close.assert_awaited()


async def test_setup_retry_on_connection_error(hass: HomeAssistant, boxes: dict[str, FakeBox]) -> None:
    boxes[GATEWAY_HOST].mock.get_channels = AsyncMock(side_effect=SkyQConnectionError("reset"))
    entry = await setup_entry(hass, make_entry())
    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_wrong_device_at_address(hass: HomeAssistant) -> None:
    entry = await setup_entry(hass, make_entry(serial="SOMEONE-ELSE"))
    assert entry.state is ConfigEntryState.SETUP_ERROR


# ------------------------------------------------------------------ coordinator


async def test_standby_and_wake(hass: HomeAssistant, boxes: dict[str, FakeBox], freezer: FrozenDateTimeFactory) -> None:
    entry = await setup_entry(hass, make_entry())
    coordinator = entry.runtime_data
    box = boxes[GATEWAY_HOST]
    assert coordinator.update_interval == timedelta(seconds=5)

    box.power, box.uri = PowerState.STANDBY, None
    freezer.tick(timedelta(seconds=6))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert coordinator.data.power is PowerState.STANDBY and coordinator.data.channel is None
    assert coordinator.update_interval == timedelta(seconds=30)

    box.power, box.uri = PowerState.ON, "xsi://BC0"
    box.push_system_status("active")  # standby -> active push triggers an immediate poll
    box.push_system_status("standby")
    box.push_system_status("active")
    await hass.async_block_till_done()
    freezer.tick(timedelta(seconds=11))  # past the refresh debouncer
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert coordinator.data.power is PowerState.ON and coordinator.data.channel is not None
    assert coordinator.data.system_status and coordinator.data.system_status.is_active


async def test_programme_cached_until_channel_changes(hass: HomeAssistant, boxes: dict[str, FakeBox]) -> None:
    entry = await setup_entry(hass, make_entry())
    box = boxes[GATEWAY_HOST]
    calls = box.mock.epg.now_and_next.await_count
    await entry.runtime_data.async_refresh()
    assert box.mock.epg.now_and_next.await_count == calls  # same channel, programme still on

    box.uri = "xsi://81B"  # BBC Two HD (2075)
    await entry.runtime_data.async_refresh()
    assert box.mock.epg.now_and_next.await_count == calls + 1
    assert entry.runtime_data.data.channel and entry.runtime_data.data.channel.sid == 2075


async def test_recording_and_app_playback(
    hass: HomeAssistant, boxes: dict[str, FakeBox], freezer: FrozenDateTimeFactory
) -> None:
    entry = await setup_entry(hass, make_entry())
    box = boxes[GATEWAY_HOST]
    box.uri = "file://pvr/2902aeb9"
    await entry.runtime_data.async_refresh()
    data = entry.runtime_data.data
    assert data.recording and data.recording.title == "Recording P2902aeb9"
    assert data.position and data.position.position == 600 and data.channel is None

    box.uri = None
    box.push_app("YouTube")
    await hass.async_block_till_done()
    freezer.tick(timedelta(seconds=11))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    data = entry.runtime_data.data
    assert data.visible_app == "YouTube" and data.recording is None and data.channel is None


async def test_pvr_and_channel_list_pushes_refetch(
    hass: HomeAssistant, boxes: dict[str, FakeBox], freezer: FrozenDateTimeFactory
) -> None:
    await setup_entry(hass, make_entry())
    box = boxes[GATEWAY_HOST]
    recordings, channels = box.mock.get_recordings.await_count, box.mock.get_channels.await_count
    box.push_pvr()
    box.push_channel_list()
    await hass.async_block_till_done()
    freezer.tick(timedelta(seconds=11))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert box.mock.get_recordings.await_count == recordings + 1
    assert box.mock.get_channels.await_count == channels + 1


async def test_guide_and_soap_failures_are_tolerated(hass: HomeAssistant, boxes: dict[str, FakeBox]) -> None:
    from custom_components.skyq_control.aioskyq import SkyQSoapError

    box = boxes[GATEWAY_HOST].mock
    box.epg.now_and_next = AsyncMock(side_effect=SkyQConnectionError("guide down"))
    entry = await setup_entry(hass, make_entry())
    assert entry.state is ConfigEntryState.LOADED
    assert entry.runtime_data.data.channel is not None and entry.runtime_data.data.programme is None

    box.get_media_info = AsyncMock(side_effect=SkyQSoapError("GetMediaInfo", 501, "Action Failed"))
    await entry.runtime_data.async_refresh()
    assert entry.runtime_data.last_update_success and entry.runtime_data.data.media is None


async def test_update_failure_marks_unavailable(hass: HomeAssistant, boxes: dict[str, FakeBox]) -> None:
    entry = await setup_entry(hass, make_entry())
    boxes[GATEWAY_HOST].mock.get_transport_info = AsyncMock(side_effect=SkyQConnectionError("gone"))
    await entry.runtime_data.async_refresh()
    assert not entry.runtime_data.last_update_success


async def test_push_during_poll_is_not_overwritten(hass: HomeAssistant, boxes: dict[str, FakeBox]) -> None:
    """A push that lands while a poll is awaiting the box must survive the poll's result."""
    import asyncio

    entry = await setup_entry(hass, make_entry())
    box = boxes[GATEWAY_HOST]
    gate = asyncio.Event()
    original = box.mock.get_transport_info.side_effect

    async def slow_transport():  # type: ignore[no-untyped-def]
        await gate.wait()
        return original()

    box.mock.get_transport_info = AsyncMock(side_effect=slow_transport)
    refresh = hass.async_create_task(entry.runtime_data.async_refresh())
    await asyncio.sleep(0)
    box.push_app("Netflix")  # arrives mid-poll
    gate.set()
    await refresh
    assert entry.runtime_data.data.visible_app == "Netflix"


# ------------------------------------------------------------------ diagnostics


async def test_diagnostics_redacted(hass: HomeAssistant, hass_client: ClientSessionGenerator) -> None:
    entry = await setup_entry(hass, make_entry())
    diagnostics = await get_diagnostics_for_config_entry(hass, hass_client, entry)
    assert diagnostics["entry"]["unique_id"] == "**REDACTED**"
    assert diagnostics["device"]["serialNumber"] == "**REDACTED**"
    assert diagnostics["device"]["MACAddress"] == "**REDACTED**"
    assert diagnostics["device"]["hardwareModel"] == "ES240"
    assert diagnostics["playback"]["channel"]["c"] == "101"
    assert diagnostics["pvr"]["total"] == 7 and diagnostics["catalogue"]["channels"] == 377
    assert diagnostics["coordinator"]["fetches_pvr"] is True
    flat = str(diagnostics)
    assert GATEWAY_SERIAL not in flat and "00:11:22:33:44" not in flat
