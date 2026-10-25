"""Box events: detection, the bus event, event entities and device triggers."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock

import pytest
from homeassistant.components import automation
from homeassistant.components.device_automation import DeviceAutomationType
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import device_registry as dr
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import (
    async_capture_events,
    async_get_device_automations,
    async_mock_service,
)

from custom_components.skyq_control.aioskyq import PowerState, RecordingList
from custom_components.skyq_control.const import DOMAIN
from custom_components.skyq_control.coordinator import SkyQData
from custom_components.skyq_control.events import EVENT_TYPE, box_events

from .conftest import GATEWAY_HOST, GATEWAY_SERIAL, MINI_HOST, MINI_SERIAL, FakeBox, load, make_entry, setup_entry

pytestmark = pytest.mark.usefixtures("mock_skyq")

NOW = int(datetime.now(UTC).timestamp())


def pvr(*items: dict[str, Any]) -> RecordingList:
    return RecordingList.from_api({"pvrItems": list(items)})


def item(pvrid: str, status: str, **extra: Any) -> dict[str, Any]:
    return {"pvrid": pvrid, "t": f"Show {pvrid}", "cn": "BBC One", "status": status, "st": NOW, **extra}


async def refresh(hass: HomeAssistant, entry_index: int = 0) -> None:
    await hass.config_entries.async_entries(DOMAIN)[entry_index].runtime_data.async_refresh()
    await hass.async_block_till_done()


def gateway_device(hass: HomeAssistant, serial: str = GATEWAY_SERIAL) -> dr.DeviceEntry:
    entry = next(e for e in hass.config_entries.async_entries(DOMAIN) if e.unique_id == serial)
    device = dr.async_get(hass).async_get_device_by_identifier((DOMAIN, serial), entry.entry_id)
    assert device is not None
    return device


# ------------------------------------------------------------------ detection


async def test_box_events_detection(hass: HomeAssistant) -> None:
    entry = await setup_entry(hass, make_entry())
    base: SkyQData = entry.runtime_data.data
    other = next(c for c in base.channels.values() if c.number == "102")

    def events(old: SkyQData, new: SkyQData, recordings: bool = True) -> list[tuple[str, dict[str, Any]]]:
        return list(box_events(old, new, recordings=recordings))

    assert events(base, replace(base)) == []
    changed = events(base, replace(base, channel=other))
    assert [e[0] for e in changed] == ["channel_changed"] and changed[0][1]["channel_number"] == "102"
    standby = replace(base, power=PowerState.STANDBY, channel=None)
    assert events(base, standby) == []  # turning off is the media player's own trigger
    assert [e[0] for e in events(standby, base)] == ["channel_changed"]  # back on: now showing this

    in_app = replace(base, visible_app="Netflix")
    assert events(base, in_app) == [("app_changed", {"app_id": "Netflix", "app_name": "Netflix"})]
    assert events(in_app, base) == [("app_changed", {"app_id": None, "app_name": None})]
    assert events(base, replace(base, visible_app="com.bskyb.epgui")) == []  # Sky's TV UI isn't an app

    old = replace(base, recordings=pvr(item("A", "SCHEDULED"), item("B", "RECORDING"), item("C", "SCHEDULED")))
    new = replace(
        base,
        recordings=pvr(
            item("A", "RECORDING"),
            item("B", "RECORDED"),
            item("C", "FAILED", fr="No Signal"),
            item("D", "FAILED", fr="N/A", **{"del": NOW}),  # already in the trash: ignored
        ),
    )
    fired = events(old, new)
    assert [(t, d["recording_id"]) for t, d in fired] == [
        ("recording_started", "A"),
        ("recording_finished", "B"),
        ("recording_failed", "C"),
    ]
    assert fired[2][1]["failure_reason"] == "No Signal"
    assert events(old, new, recordings=False) == []  # a Mini whose gateway holds the recordings
    assert events(replace(base, recordings=None), new) == []  # nothing to compare with yet


# ------------------------------------------------------------------ bus events and event entities


async def test_events_fire_on_bus_and_entities(hass: HomeAssistant, boxes: dict[str, FakeBox]) -> None:
    await setup_entry(hass, make_entry())
    fired = async_capture_events(hass, EVENT_TYPE)
    device_id = gateway_device(hass).id
    box = boxes[GATEWAY_HOST]

    box.uri = "xsi://81B"  # sid 2075
    await refresh(hass)
    assert len(fired) == 1
    assert fired[0].data["device_id"] == device_id and fired[0].data["type"] == "channel_changed"
    playback = hass.states.get("event.sky_q_gateway_playback")
    assert playback.attributes["event_type"] == "channel_changed"
    assert playback.attributes["service_id"] == 2075

    box.push_app("YouTube")
    await hass.async_block_till_done()
    assert fired[-1].data["type"] == "app_changed" and fired[-1].data["app_name"] == "YouTube"
    assert hass.states.get("event.sky_q_gateway_playback").attributes["app_id"] == "YouTube"

    items = load("rest/pvr_list.json")["pvrItems"]
    box.mock.get_recordings = AsyncMock(return_value=pvr(*items, item("Pnew", "RECORDING")))
    box.push_pvr()
    await hass.async_block_till_done()
    await refresh(hass)
    started = [e for e in fired if e.data["type"] == "recording_started"]
    assert len(started) == 1 and started[0].data["recording_id"] == "Pnew"
    recordings = hass.states.get("event.sky_q_gateway_recordings")
    assert recordings.attributes["event_type"] == "recording_started"
    assert recordings.attributes["title"] == "Show Pnew"


async def test_nothing_fired_at_startup(hass: HomeAssistant) -> None:
    fired = async_capture_events(hass, EVENT_TYPE)
    await setup_entry(hass, make_entry())
    await refresh(hass)
    assert fired == []
    assert hass.states.get("event.sky_q_gateway_playback").state == "unknown"


async def test_mini_has_no_recordings_events(hass: HomeAssistant) -> None:
    await setup_entry(hass, make_entry())
    await setup_entry(hass, make_entry(MINI_HOST, MINI_SERIAL))
    mini = f"sky_q_mini_{MINI_HOST.replace('.', '_')}"
    assert hass.states.get(f"event.{mini}_playback") is not None
    assert hass.states.get(f"event.{mini}_recordings") is None


# ------------------------------------------------------------------ device triggers


async def test_device_triggers_listed(hass: HomeAssistant) -> None:
    await setup_entry(hass, make_entry())
    await setup_entry(hass, make_entry(MINI_HOST, MINI_SERIAL))
    ours = lambda triggers: sorted(t["type"] for t in triggers if t["domain"] == DOMAIN)  # noqa: E731
    gateway = await async_get_device_automations(hass, DeviceAutomationType.TRIGGER, gateway_device(hass).id)
    assert ours(gateway) == [
        "app_changed",
        "channel_changed",
        "recording_failed",
        "recording_finished",
        "recording_started",
    ]
    mini = await async_get_device_automations(hass, DeviceAutomationType.TRIGGER, gateway_device(hass, MINI_SERIAL).id)
    assert ours(mini) == ["app_changed", "channel_changed"]


async def test_device_trigger_runs_automation(hass: HomeAssistant, boxes: dict[str, FakeBox]) -> None:
    await setup_entry(hass, make_entry())
    calls: list[ServiceCall] = async_mock_service(hass, "test", "automation")
    device_id = gateway_device(hass).id
    assert await async_setup_component(
        hass,
        automation.DOMAIN,
        {
            automation.DOMAIN: [
                {
                    "trigger": {"platform": "device", "domain": DOMAIN, "device_id": device_id, "type": t},
                    "action": {"service": "test.automation", "data_template": {"what": t, "info": info}},
                }
                for t, info in (
                    ("channel_changed", "{{ trigger.event.data.channel }}"),
                    ("app_changed", "{{ trigger.event.data.app_name }}"),
                )
            ]
        },
    )
    box = boxes[GATEWAY_HOST]
    box.push_app("Netflix")
    await hass.async_block_till_done()
    box.push_app(None)
    box.uri = "xsi://81B"
    await refresh(hass)
    assert [(c.data["what"], c.data["info"]) for c in calls] == [
        ("app_changed", "Netflix"),
        ("app_changed", None),  # back to live TV
        ("channel_changed", "102 BBC Two HD"),
    ]
