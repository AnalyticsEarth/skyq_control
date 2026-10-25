"""Sensors, binary sensors and images."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from custom_components.skyq_control.aioskyq import PowerState, RecordingList, SystemStatus
from custom_components.skyq_control.const import DOMAIN
from custom_components.skyq_control.coordinator import SkyQCoordinator

from .conftest import (
    GATEWAY_HOST,
    GATEWAY_SERIAL,
    MINI_HOST,
    MINI_SERIAL,
    FakeBox,
    device_info,
    load,
    make_entry,
    setup_entry,
)

pytestmark = pytest.mark.usefixtures("mock_skyq")

GW = "sky_q_gateway"
MINI = f"sky_q_mini_{MINI_HOST.replace('.', '_')}"


def state(hass: HomeAssistant, entity_id: str) -> str:
    entity = hass.states.get(entity_id)
    assert entity is not None, entity_id
    return entity.state


async def refresh(hass: HomeAssistant, index: int = 0) -> None:
    await hass.config_entries.async_entries(DOMAIN)[index].runtime_data.async_refresh()
    await hass.async_block_till_done()


# ------------------------------------------------------------------ sensors


async def test_whats_on(hass: HomeAssistant, boxes: dict[str, FakeBox]) -> None:
    await setup_entry(hass, make_entry())
    assert state(hass, f"sensor.{GW}_channel") == "101 BBC One SE HD"
    assert hass.states.get(f"sensor.{GW}_channel").attributes["service_id"] == 3008
    programme = hass.states.get(f"sensor.{GW}_programme")
    assert programme.state == "Kingdom" and programme.attributes["event_id"] == "Ebc0-64"
    assert state(hass, f"sensor.{GW}_next_programme") == "News"
    ends = dt_util.parse_datetime(state(hass, f"sensor.{GW}_programme_ends"))
    assert ends - dt_util.utcnow() < timedelta(minutes=21)
    assert state(hass, f"sensor.{GW}_playback") == "playing"
    assert state(hass, f"sensor.{GW}_app") == STATE_UNKNOWN

    box = boxes[GATEWAY_HOST]
    box.push_app("Netflix")
    await hass.async_block_till_done()
    assert state(hass, f"sensor.{GW}_app") == "Netflix"

    box.uri = "file://pvr/2902aeb9"  # playing a recording
    box.push_app(None)
    await refresh(hass)
    assert state(hass, f"sensor.{GW}_programme") == "Recording P2902aeb9"
    assert hass.states.get(f"sensor.{GW}_programme").attributes["recording_id"] == "P2902aeb9"
    assert state(hass, f"sensor.{GW}_programme_ends") == STATE_UNKNOWN

    box.power = PowerState.STANDBY
    await refresh(hass)
    for key in ("channel", "programme", "next_programme", "playback"):
        assert state(hass, f"sensor.{GW}_{key}") == STATE_UNKNOWN


async def test_recorder(hass: HomeAssistant, boxes: dict[str, FakeBox]) -> None:
    await setup_entry(hass, make_entry())
    assert state(hass, f"sensor.{GW}_disk_usage") == "92.2"
    assert float(state(hass, f"sensor.{GW}_disk_free")) == pytest.approx(125.966, abs=0.01)  # MB shown as GB
    assert state(hass, f"sensor.{GW}_recordings") == "5"  # recorded + part recorded
    assert state(hass, f"sensor.{GW}_failed_recordings") == "2"
    assert state(hass, f"sensor.{GW}_scheduled_recordings") == "0"
    assert state(hass, f"sensor.{GW}_deleted_recordings") == "0"
    assert state(hass, f"sensor.{GW}_recording_now") == "0"
    assert state(hass, f"sensor.{GW}_next_recording") == STATE_UNKNOWN
    assert state(hass, f"binary_sensor.{GW}_recording") == STATE_OFF
    assert state(hass, f"sensor.{GW}_pin_tries_left") == "3"

    # One recording now, one booked for tomorrow, one in the trash.
    items = load("rest/pvr_list.json")["pvrItems"]
    now = int(datetime.now(UTC).timestamp())
    items[0] |= {"status": "RECORDING", "st": now - 600, "schd": 3600}
    items[1] |= {"status": "SCHEDULED", "st": now + 86400, "schd": 3600}
    items[2] |= {"del": now - 60}
    box = boxes[GATEWAY_HOST]
    box.mock.get_recordings.return_value = RecordingList.from_api({"pvrItems": items})
    box.push_pvr()
    await hass.async_block_till_done()
    await refresh(hass)

    assert state(hass, f"sensor.{GW}_recording_now") == "1"
    assert hass.states.get(f"sensor.{GW}_recording_now").attributes["titles"] == [items[0]["t"]]
    assert state(hass, f"binary_sensor.{GW}_recording") == STATE_ON
    assert state(hass, f"sensor.{GW}_scheduled_recordings") == "1"
    assert state(hass, f"sensor.{GW}_deleted_recordings") == "1"
    upcoming = hass.states.get(f"sensor.{GW}_next_recording")
    assert dt_util.parse_datetime(upcoming.state) == datetime.fromtimestamp(now + 86400, UTC)
    assert upcoming.attributes["title"] == items[1]["t"]


async def test_box_status(hass: HomeAssistant, boxes: dict[str, FakeBox], entity_registry: er.EntityRegistry) -> None:
    await setup_entry(hass, make_entry())
    # system/status arrives by push just after setup
    assert state(hass, f"sensor.{GW}_guide_available_until") == STATE_UNAVAILABLE
    assert state(hass, f"binary_sensor.{GW}_network") == STATE_UNAVAILABLE
    boxes[GATEWAY_HOST].push_system_status()
    await hass.async_block_till_done()

    assert state(hass, f"sensor.{GW}_software_version") == "Q370.000.09.00-AS_37.2.1"
    assert state(hass, f"sensor.{GW}_firmware_build") == "32B12C"
    assert state(hass, f"sensor.{GW}_wake_reason") == "POWERON"
    assert state(hass, f"sensor.{GW}_bouquet") == "4101/1"
    boot = dt_util.parse_datetime(state(hass, f"sensor.{GW}_last_boot"))
    assert abs((dt_util.utcnow() - boot).total_seconds() - 20572) < 60
    assert state(hass, f"sensor.{GW}_guide_available_until") == "2026-11-01"
    update = hass.states.get(f"sensor.{GW}_software_update")
    assert update.state == "idle" and update.attributes["state"] == "unavailable"
    entitlements = hass.states.get(f"sensor.{GW}_entitlements")
    assert entitlements.state == "7" and "ULTRA+" in entitlements.attributes["entitlements"]
    assert state(hass, f"sensor.{GW}_smartcard") == "available"
    assert entity_registry.async_get(f"sensor.{GW}_household_id").disabled
    assert entity_registry.async_get(f"sensor.{GW}_viewing_card_number").disabled
    assert hass.states.get(f"sensor.{GW}_gateway_address") is None  # Minis only

    for key, expected in (
        ("power", STATE_ON),
        ("watching_a_recording", STATE_OFF),
        ("network", STATE_ON),
        ("recorder", STATE_OFF),  # problem sensors: off means OK
        ("drm", STATE_OFF),
        ("guide_data", STATE_OFF),
        ("software_update", STATE_OFF),
        ("smartcard_active", STATE_ON),
        ("ultra_hd_capable", STATE_ON),
        ("picture_in_picture_capable", STATE_ON),
    ):
        assert state(hass, f"binary_sensor.{GW}_{key}") == expected, key


async def test_standby_and_recording_playback(hass: HomeAssistant, boxes: dict[str, FakeBox]) -> None:
    await setup_entry(hass, make_entry())
    box = boxes[GATEWAY_HOST]
    box.uri = "file://pvr/2902aeb9"
    await refresh(hass)
    assert state(hass, f"binary_sensor.{GW}_watching_a_recording") == STATE_ON
    box.power = PowerState.STANDBY
    await refresh(hass)
    assert state(hass, f"binary_sensor.{GW}_power") == STATE_OFF
    assert state(hass, f"binary_sensor.{GW}_watching_a_recording") == STATE_OFF


async def test_mini_with_gateway(hass: HomeAssistant, boxes: dict[str, FakeBox]) -> None:
    await setup_entry(hass, make_entry())
    await setup_entry(hass, make_entry(MINI_HOST, MINI_SERIAL))
    boxes[MINI_HOST].push_system_status()
    await hass.async_block_till_done()
    assert state(hass, f"sensor.{MINI}_gateway_address") == GATEWAY_HOST
    assert state(hass, f"binary_sensor.{MINI}_gateway_connection") == STATE_ON

    # In standby a Mini suspends its gateway link, and says why.
    status = load("mini/ws/system_status.json")
    status["gatewayservices"] |= {"state": "unavailable", "reason": "connection suspended for low power mode"}
    boxes[MINI_HOST].listeners["system_status"](SystemStatus.from_api(status))
    await hass.async_block_till_done()
    link = hass.states.get(f"binary_sensor.{MINI}_gateway_connection")
    assert link.state == STATE_OFF and link.attributes["reason"] == "connection suspended for low power mode"
    # Recordings belong to the gateway's device.
    assert hass.states.get(f"sensor.{MINI}_disk_usage") is None
    assert hass.states.get(f"binary_sensor.{MINI}_recording") is None
    assert hass.states.get(f"sensor.{GW}_disk_usage") is not None


async def test_mini_alone_then_gateway_added(hass: HomeAssistant) -> None:
    await setup_entry(hass, make_entry(MINI_HOST, MINI_SERIAL))
    assert state(hass, f"sensor.{MINI}_recordings") == "5"
    await setup_entry(hass, make_entry())
    # The gateway now owns the recordings; the Mini's copies go unavailable until it's reloaded.
    assert state(hass, f"sensor.{MINI}_recordings") == STATE_UNAVAILABLE
    assert state(hass, f"sensor.{GW}_recordings") == "5"

    mini = hass.config_entries.async_entries(DOMAIN)[0]
    await hass.config_entries.async_reload(mini.entry_id)
    await hass.async_block_till_done()
    registry = er.async_get(hass)
    assert registry.async_get(f"sensor.{MINI}_recordings") is None  # removed, not left unavailable
    assert registry.async_get(f"binary_sensor.{MINI}_recording") is None
    assert registry.async_get(f"sensor.{MINI}_software_version") is not None


def test_boot_time_is_steady() -> None:
    device = device_info("gateway", GATEWAY_HOST, GATEWAY_SERIAL)
    first = SkyQCoordinator._boot_time(device, None)
    assert first is not None and first.microsecond == 0
    later = replace(device, system_uptime=device.system_uptime - 30)  # the next read lands 30 s off
    assert SkyQCoordinator._boot_time(later, first) == first
    rebooted = replace(device, system_uptime=60)
    assert SkyQCoordinator._boot_time(rebooted, first) != first
    assert SkyQCoordinator._boot_time(replace(device, system_uptime=None), first) is None


# ------------------------------------------------------------------ images


async def test_images(hass: HomeAssistant, boxes: dict[str, FakeBox]) -> None:
    await setup_entry(hass, make_entry())
    artwork_id, logo_id = f"image.{GW}_programme_artwork", f"image.{GW}_channel_logo"
    component = hass.data["image"]
    artwork, logo = component.get_entity(artwork_id), component.get_entity(logo_id)
    assert artwork.image_url.startswith("https://") and "8649a421" in artwork.image_url
    assert logo.image_url.endswith("skychb3008.png")
    first_update = state(hass, artwork_id)
    assert first_update not in (STATE_UNKNOWN, STATE_UNAVAILABLE)

    box = boxes[GATEWAY_HOST]
    box.uri = "xsi://81B"  # another channel (sid 2075)
    await refresh(hass)
    assert logo.image_url.endswith("skychb2075.png")

    box.uri = None  # an app, nothing on the tuner
    await refresh(hass)
    assert state(hass, artwork_id) == STATE_UNAVAILABLE
    assert state(hass, logo_id) == STATE_UNAVAILABLE
