"""TV guide: the media browser, the get_guide and record actions, and the shared guide client."""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from unittest.mock import MagicMock

import pytest
from homeassistant.components.media_player import DOMAIN as MP_DOMAIN
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.typing import WebSocketGenerator

from custom_components.skyq_control.aioskyq import SkyQConnectionError
from custom_components.skyq_control.const import DOMAIN

from .conftest import GATEWAY_HOST, MINI_HOST, MINI_SERIAL, FakeBox, make_entry, setup_entry

pytestmark = pytest.mark.usefixtures("mock_skyq")

PLAYER = "media_player.sky_q_gateway"
MINI_PLAYER = f"media_player.sky_q_mini_{MINI_HOST.replace('.', '_')}"


async def browse(
    hass_ws_client: WebSocketGenerator, hass: HomeAssistant, content_id: str | None = None, player: str = PLAYER
) -> dict[str, Any]:
    client = await hass_ws_client(hass)
    message: dict[str, Any] = {"type": "media_player/browse_media", "entity_id": player}
    if content_id is not None:
        message |= {"media_content_id": content_id, "media_content_type": "channel"}
    await client.send_json_auto_id(message)
    response = await client.receive_json()
    assert response["success"], response
    return response["result"]


async def get_guide(hass: HomeAssistant, **data: Any) -> list[dict[str, Any]]:
    response = await hass.services.async_call(
        DOMAIN, "get_guide", {ATTR_ENTITY_ID: PLAYER, **data}, blocking=True, return_response=True
    )
    return response[PLAYER]["programmes"]


# ------------------------------------------------------------------ media browser


async def test_browse_root(hass: HomeAssistant, hass_ws_client: WebSocketGenerator) -> None:
    await setup_entry(hass, make_entry())
    root = await browse(hass_ws_client, hass)
    titles = [child["title"] for child in root["children"]]
    assert titles[0] == "Favourites" and titles[1] == "Entertainment" and titles[-1] == "Apps"
    assert "Movies" in titles and "Sports" in titles and "Radio" in titles


async def test_browse_mini_has_no_favourites_folder(hass: HomeAssistant, hass_ws_client: WebSocketGenerator) -> None:
    await setup_entry(hass, make_entry(MINI_HOST, MINI_SERIAL))
    root = await browse(hass_ws_client, hass, player=MINI_PLAYER)
    assert root["children"][0]["title"] == "Entertainment"


async def test_browse_channels_show_whats_on(hass: HomeAssistant, hass_ws_client: WebSocketGenerator) -> None:
    await setup_entry(hass, make_entry())
    favourites = await browse(hass_ws_client, hass, "favourites")
    first = favourites["children"][0]
    assert first["title"] == "101 BBC One SE HD · Kingdom"
    assert first["media_content_id"] == "sid:3008" and first["can_play"] and first["can_expand"]
    assert first["thumbnail"].endswith("skychb3008.png")

    entertainment = await browse(hass_ws_client, hass, "genre:12")
    assert len(entertainment["children"]) > len(favourites["children"])
    assert all(" · Kingdom" in child["title"] for child in entertainment["children"] if child["can_expand"])


async def test_browse_without_guide_lists_channels(
    hass: HomeAssistant, hass_ws_client: WebSocketGenerator, guide: MagicMock
) -> None:
    await setup_entry(hass, make_entry())
    guide.get_schedule.side_effect = SkyQConnectionError("guide down")
    favourites = await browse(hass_ws_client, hass, "favourites")
    assert favourites["children"][0]["title"] == "101 BBC One SE HD"


async def test_browse_channel_schedule(hass: HomeAssistant, hass_ws_client: WebSocketGenerator) -> None:
    await setup_entry(hass, make_entry())
    schedule = await browse(hass_ws_client, hass, "sid:3008")
    programmes = schedule["children"]
    assert schedule["can_play"] and len(programmes) == 48
    assert programmes[0]["title"] == "Now · Kingdom" and programmes[0]["can_play"]
    assert programmes[0]["media_content_id"] == "sid:3008"
    upcoming = programmes[1]
    start = dt_util.as_local(dt_util.utcnow().replace(second=0, microsecond=0) + timedelta(minutes=20))
    assert upcoming["title"].endswith(f"{start:%H:%M} · News")
    assert not upcoming["can_play"] and upcoming["media_content_id"].startswith("event:")


async def test_browse_apps_and_play(
    hass: HomeAssistant, hass_ws_client: WebSocketGenerator, boxes: dict[str, FakeBox]
) -> None:
    await setup_entry(hass, make_entry())
    apps = await browse(hass_ws_client, hass, "apps")
    assert [a["title"] for a in apps["children"]] == ["Apple TV", "Disney+", "Netflix", "Services", "YouTube"]

    # What the browser sends when a channel or app is played.
    for content_type, content_id in (("channel", "sid:3008"), ("app", "apple.tv")):
        await hass.services.async_call(
            MP_DOMAIN,
            "play_media",
            {ATTR_ENTITY_ID: PLAYER, "media_content_type": content_type, "media_content_id": content_id},
            blocking=True,
        )
    boxes[GATEWAY_HOST].mock.tune.assert_awaited_with(3008)
    boxes[GATEWAY_HOST].mock.launch_app.assert_awaited_with("apple.tv")


async def test_browse_unknown(hass: HomeAssistant, hass_ws_client: WebSocketGenerator) -> None:
    await setup_entry(hass, make_entry())
    client = await hass_ws_client(hass)
    await client.send_json_auto_id(
        {
            "type": "media_player/browse_media",
            "entity_id": PLAYER,
            "media_content_id": "sid:1",
            "media_content_type": "channel",
        }
    )
    response = await client.receive_json()
    assert not response["success"]


# ------------------------------------------------------------------ get_guide and record


async def test_get_guide_current_channel(hass: HomeAssistant) -> None:
    await setup_entry(hass, make_entry())
    programmes = await get_guide(hass, hours=1)
    assert [p["title"] for p in programmes] == ["Kingdom", "News", "Show 2"]
    first = programmes[0]
    assert first["channel"] == "BBC One SE HD" and first["channel_number"] == "101" and first["service_id"] == 3008
    assert first["event_id"] == "Ebc0-64" and first["image"]
    assert dt_util.parse_datetime(first["start"]) < dt_util.utcnow() < dt_util.parse_datetime(first["end"])


async def test_get_guide_channels_and_window(hass: HomeAssistant, guide: MagicMock) -> None:
    await setup_entry(hass, make_entry())
    start = dt_util.now() + timedelta(hours=3)
    programmes = await get_guide(hass, channels="102, sid:3008, BBC Two HD", start=start.isoformat(), hours=1)
    assert {p["channel_number"] for p in programmes} == {"101", "102"}  # 102 named twice, fetched once
    assert len(programmes) == 6
    assert [p["start"] for p in programmes] == sorted(p["start"] for p in programmes)
    channels, window_start, window_end = guide.get_schedule.await_args.args
    assert len(channels) == 2 and window_end - window_start == timedelta(hours=1)


async def test_get_guide_search(hass: HomeAssistant, guide: MagicMock) -> None:
    await setup_entry(hass, make_entry())
    programmes = await get_guide(hass, search="kingdom", hours=2)
    assert programmes and all(p["title"] == "Kingdom" for p in programmes)
    assert len(guide.get_schedule.await_args.args[0]) > 300  # searched every channel with a guide


async def test_get_guide_errors(hass: HomeAssistant, boxes: dict[str, FakeBox], guide: MagicMock) -> None:
    await setup_entry(hass, make_entry())
    with pytest.raises(ServiceValidationError, match="999"):
        await get_guide(hass, channels=["999"])

    guide.get_schedule.side_effect = SkyQConnectionError("HTTP 503")
    with pytest.raises(HomeAssistantError, match="HTTP 503"):
        await get_guide(hass)

    boxes[GATEWAY_HOST].uri = None  # nothing on screen
    await hass.config_entries.async_entries(DOMAIN)[0].runtime_data.async_refresh()
    with pytest.raises(ServiceValidationError, match="channels"):
        await get_guide(hass)


async def test_record(hass: HomeAssistant, boxes: dict[str, FakeBox]) -> None:
    await setup_entry(hass, make_entry())
    box = boxes[GATEWAY_HOST].mock
    await hass.services.async_call(DOMAIN, "record", {ATTR_ENTITY_ID: PLAYER, "event_id": " Ebc0-82 "}, blocking=True)
    box.book_recording.assert_awaited_with("Ebc0-82", series=False)
    await hass.services.async_call(
        DOMAIN, "record", {ATTR_ENTITY_ID: PLAYER, "event_id": "Ebc0-82", "series": True}, blocking=True
    )
    box.book_recording.assert_awaited_with("Ebc0-82", series=True)

    box.book_recording.side_effect = SkyQConnectionError("refused")
    with pytest.raises(HomeAssistantError, match="refused"):
        await hass.services.async_call(DOMAIN, "record", {ATTR_ENTITY_ID: PLAYER, "event_id": "x"}, blocking=True)


async def test_next_event_id_attribute(hass: HomeAssistant) -> None:
    await setup_entry(hass, make_entry())
    assert hass.states.get(PLAYER).attributes["next_event_id"] == "Ebc0-82"


async def test_boxes_share_one_guide_client(hass: HomeAssistant, mock_skyq: MagicMock) -> None:
    from custom_components.skyq_control import GUIDE_CLIENTS

    await setup_entry(hass, make_entry())
    await setup_entry(hass, make_entry(MINI_HOST, MINI_SERIAL))
    assert list(hass.data[GUIDE_CLIENTS]) == ["GB"]
