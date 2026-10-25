"""Recordings: management actions, lookups, calendars and the media browser folder."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock

import pytest
from homeassistant.const import ATTR_ENTITY_ID, STATE_OFF, STATE_ON, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.typing import WebSocketGenerator

from custom_components.skyq_control.aioskyq import RecordingList, SkyQConnectionError
from custom_components.skyq_control.const import DOMAIN

from .conftest import GATEWAY_HOST, MINI_HOST, MINI_SERIAL, FakeBox, load, make_entry, setup_entry

pytestmark = pytest.mark.usefixtures("mock_skyq")

PLAYER = "media_player.sky_q_gateway"
MINI_PLAYER = f"media_player.sky_q_mini_{MINI_HOST.replace('.', '_')}"
RECORDED = "P2902aeb9"  # in rest/pvr_list.json
NOW = int(datetime.now(UTC).timestamp())


@pytest.fixture
def household(boxes: dict[str, FakeBox]) -> list[dict[str, Any]]:
    """The fixture's recordings plus one recording now, two bookings and one in the trash."""
    items = load("rest/pvr_list.json")["pvrItems"]
    extra = [
        {"pvrid": "Pnow", "t": "Live Match", "cn": "Sky Sports", "status": "RECORDING", "st": NOW - 600,
         "ast": NOW - 600, "schd": 3600, "oeid": "E1-1"},
        {"pvrid": "Psoon", "t": "Film Tonight", "cn": "Sky Cinema", "status": "SCHEDULED", "st": NOW + 3600,
         "schd": 7200, "link": True, "sy": "A film."},
        {"pvrid": "Plater", "t": "Quiz", "cn": "BBC One", "status": "SCHEDULED", "st": NOW + 86400, "schd": 1800},
        {"pvrid": "Ptrash", "t": "Old Show", "cn": "ITV1", "status": "RECORDED", "st": NOW - 86400 * 3,
         "ast": NOW - 86400 * 3, "finald": 1800, "del": NOW - 3600},
    ]  # fmt: skip
    recordings = RecordingList.from_api({"pvrItems": [*items, *extra]})
    for box in boxes.values():
        box.mock.get_recordings = AsyncMock(return_value=recordings)
        for name in (
            "delete_recording",
            "erase_recording",
            "undelete_recording",
            "stop_recording",
            "set_keep",
            "set_lock",
            "set_series_link",
            "set_resume_position",
        ):
            setattr(box.mock, name, AsyncMock())
    return [*items, *extra]


async def action(hass: HomeAssistant, name: str, player: str = PLAYER, **data: Any) -> Any:
    response = name.startswith("get_")
    result = await hass.services.async_call(
        DOMAIN, name, {ATTR_ENTITY_ID: player, **data}, blocking=True, return_response=response
    )
    return result[player] if response else None


# ------------------------------------------------------------------ management actions


async def test_delete_restore_and_stop(
    hass: HomeAssistant, boxes: dict[str, FakeBox], household: list[dict[str, Any]]
) -> None:
    await setup_entry(hass, make_entry())
    box = boxes[GATEWAY_HOST].mock
    await action(hass, "delete_recording", recording_id=f" {RECORDED} ")
    box.delete_recording.assert_awaited_once_with(RECORDED)
    box.erase_recording.assert_not_awaited()
    await action(hass, "delete_recording", recording_id="Ptrash", permanent=True)
    box.erase_recording.assert_awaited_once_with("Ptrash")
    await action(hass, "delete_recording", recording_id="Psoon")  # cancels the booking
    box.delete_recording.assert_awaited_with("Psoon")
    await action(hass, "restore_recording", recording_id="Ptrash")
    box.undelete_recording.assert_awaited_once_with("Ptrash")
    await action(hass, "stop_recording", recording_id="Pnow")
    box.stop_recording.assert_awaited_once_with("Pnow")

    with pytest.raises(ServiceValidationError, match="isn't recording"):
        await action(hass, "stop_recording", recording_id=RECORDED)
    with pytest.raises(ServiceValidationError, match="Pnope"):
        await action(hass, "delete_recording", recording_id="Pnope")
    box.delete_recording.side_effect = SkyQConnectionError("box said no")
    with pytest.raises(HomeAssistantError, match="box said no"):
        await action(hass, "delete_recording", recording_id=RECORDED)


async def test_update_recording(
    hass: HomeAssistant, boxes: dict[str, FakeBox], household: list[dict[str, Any]]
) -> None:
    await setup_entry(hass, make_entry())
    box = boxes[GATEWAY_HOST].mock
    await action(hass, "update_recording", recording_id=RECORDED, keep=True, locked=False, series_link=True)
    box.set_keep.assert_awaited_once_with(RECORDED, True)
    box.set_lock.assert_awaited_once_with(RECORDED, False)
    box.set_series_link.assert_awaited_once_with(RECORDED, True)
    box.set_resume_position.assert_not_awaited()

    current = next(i for i in household if i["pvrid"] == RECORDED).get("lp") or 0
    await action(hass, "update_recording", recording_id=RECORDED, resume_position=current + 600)
    box.set_resume_position.assert_awaited_once_with(RECORDED, current + 600)

    with pytest.raises(ServiceValidationError, match="at least one"):
        await action(hass, "update_recording", recording_id=RECORDED)
    if current:
        with pytest.raises(ServiceValidationError, match="forward"):
            await action(hass, "update_recording", recording_id=RECORDED, resume_position=current - 1)


async def test_record_now(hass: HomeAssistant, boxes: dict[str, FakeBox]) -> None:
    await setup_entry(hass, make_entry())
    box = boxes[GATEWAY_HOST]
    await action(hass, "record_now", series=True)
    box.mock.book_recording.assert_awaited_once_with("Ebc0-64", series=True)

    box.uri = None
    await hass.config_entries.async_entries(DOMAIN)[0].runtime_data.async_refresh()
    with pytest.raises(ServiceValidationError, match="nothing to record"):
        await action(hass, "record_now")


async def test_actions_from_a_mini_use_the_gateways_list(
    hass: HomeAssistant, boxes: dict[str, FakeBox], household: list[dict[str, Any]]
) -> None:
    await setup_entry(hass, make_entry())
    await setup_entry(hass, make_entry(MINI_HOST, MINI_SERIAL))
    gateway, mini = boxes[GATEWAY_HOST].mock, boxes[MINI_HOST].mock
    calls = gateway.get_recordings.await_count
    await action(hass, "update_recording", MINI_PLAYER, recording_id="Psoon", keep=True)
    mini.set_keep.assert_awaited_once_with("Psoon", True)  # the Mini passes it on to the gateway
    assert gateway.get_recordings.await_count > calls  # and the gateway's list is refreshed
    with pytest.raises(ServiceValidationError):
        await action(hass, "delete_recording", MINI_PLAYER, recording_id="Pnope")
    result = await action(hass, "get_recordings", MINI_PLAYER)
    assert result["total"] == 10


# ------------------------------------------------------------------ lookups


async def test_get_recordings(hass: HomeAssistant, household: list[dict[str, Any]]) -> None:
    await setup_entry(hass, make_entry())
    result = await action(hass, "get_recordings")
    ids = [r["recording_id"] for r in result["recordings"]]
    assert result["total"] == 10 and "Ptrash" not in ids  # deleted ones only when asked for
    assert ids[:2] == ["Psoon", "Plater"]  # bookings soonest first, then newest first
    assert ids[2] == "Pnow"
    soon = result["recordings"][0]
    assert soon["status"] == "scheduled" and soon["series_linked"] and soon["channel"] == "Sky Cinema"
    assert dt_util.parse_datetime(soon["start"]) == datetime.fromtimestamp(NOW + 3600, UTC)

    deleted = await action(hass, "get_recordings", status=["deleted"])
    assert [r["recording_id"] for r in deleted["recordings"]] == ["Ptrash"]
    failed = await action(hass, "get_recordings", status="failed")
    assert failed["total"] == 2 and all(r["failure_reason"] for r in failed["recordings"])
    found = await action(hass, "get_recordings", search="film")
    assert [r["title"] for r in found["recordings"]] == ["Film Tonight"]
    limited = await action(hass, "get_recordings", limit=3)
    assert limited["total"] == 10 and len(limited["recordings"]) == 3


async def test_get_channels_and_apps(hass: HomeAssistant) -> None:
    await setup_entry(hass, make_entry())
    channels = (await action(hass, "get_channels"))["channels"]
    assert len(channels) == 377
    first = channels[0]
    assert first == {
        "number": "101",
        "name": "BBC One SE HD",
        "service_id": 3008,
        "format": "hd",
        "genre": "Entertainment",
        "radio": False,
        "favourite": 1,
        "logo": first["logo"],
    }
    assert sum(1 for c in channels if c["favourite"]) == 10
    apps = (await action(hass, "get_apps"))["apps"]
    assert {"app_id": "Netflix", "title": "Netflix"} in apps


# ------------------------------------------------------------------ calendars


async def test_calendars(hass: HomeAssistant, household: list[dict[str, Any]]) -> None:
    await setup_entry(hass, make_entry())
    scheduled = hass.states.get("calendar.sky_q_gateway_scheduled_recordings")
    # A recording in progress is the current event, so the calendar is on.
    assert scheduled.state == STATE_ON and scheduled.attributes["message"] == "Live Match"
    made = hass.states.get("calendar.sky_q_gateway_recordings")
    assert made.state == STATE_OFF

    client_events = await hass.services.async_call(
        "calendar",
        "get_events",
        {
            ATTR_ENTITY_ID: ["calendar.sky_q_gateway_scheduled_recordings", "calendar.sky_q_gateway_recordings"],
            "start_date_time": dt_util.now() - timedelta(days=30),
            "end_date_time": dt_util.now() + timedelta(days=2),
        },
        blocking=True,
        return_response=True,
    )
    upcoming = client_events["calendar.sky_q_gateway_scheduled_recordings"]["events"]
    assert [e["summary"] for e in upcoming] == ["Live Match", "Film Tonight", "Quiz"]
    film = upcoming[1]
    assert (
        film["location"] == "Sky Cinema" and "A film." in film["description"] and "Series linked" in film["description"]
    )
    made_titles = [e["summary"] for e in client_events["calendar.sky_q_gateway_recordings"]["events"]]
    assert "Old Show" not in made_titles  # in the trash


async def test_calendars_only_on_the_recordings_box(hass: HomeAssistant, entity_registry: er.EntityRegistry) -> None:
    mini = await setup_entry(hass, make_entry(MINI_HOST, MINI_SERIAL))
    mini_calendar = f"calendar.sky_q_mini_{MINI_HOST.replace('.', '_')}_recordings"
    assert hass.states.get(mini_calendar) is not None  # no gateway set up, so the Mini holds them
    await setup_entry(hass, make_entry())
    assert hass.states.get(mini_calendar).state == STATE_UNAVAILABLE
    await hass.config_entries.async_reload(mini.entry_id)
    await hass.async_block_till_done()
    assert entity_registry.async_get(mini_calendar) is None


# ------------------------------------------------------------------ media browser


async def test_browse_recordings(
    hass: HomeAssistant, hass_ws_client: WebSocketGenerator, household: list[dict[str, Any]]
) -> None:
    await setup_entry(hass, make_entry())
    await setup_entry(hass, make_entry(MINI_HOST, MINI_SERIAL))
    client = await hass_ws_client(hass)
    for player in (PLAYER, MINI_PLAYER):  # a Mini browses the gateway's recordings
        await client.send_json_auto_id(
            {
                "type": "media_player/browse_media",
                "entity_id": player,
                "media_content_id": "recordings",
                "media_content_type": "recordings",
            }
        )
        response = await client.receive_json()
        assert response["success"], response
        children = response["result"]["children"]
        assert len(children) == 5  # recorded and part-recorded, not failed, scheduled or deleted
        assert not any(c["can_play"] for c in children)
        assert all(c["media_content_id"].startswith("recording:P") for c in children)
