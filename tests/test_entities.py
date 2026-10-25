"""Media player, remote, buttons, selects and the integration's actions."""

from __future__ import annotations

from unittest.mock import AsyncMock, call, patch

import pytest
from homeassistant.components.media_player import (
    ATTR_APP_NAME,
    ATTR_INPUT_SOURCE,
    ATTR_INPUT_SOURCE_LIST,
    ATTR_MEDIA_CHANNEL,
    ATTR_MEDIA_CONTENT_ID,
    ATTR_MEDIA_CONTENT_TYPE,
    ATTR_MEDIA_DURATION,
    ATTR_MEDIA_POSITION,
    ATTR_MEDIA_POSITION_UPDATED_AT,
    ATTR_MEDIA_TITLE,
    MediaType,
)
from homeassistant.components.media_player import DOMAIN as MP_DOMAIN
from homeassistant.components.remote import ATTR_COMMAND, ATTR_DELAY_SECS, ATTR_NUM_REPEATS
from homeassistant.components.remote import DOMAIN as REMOTE_DOMAIN
from homeassistant.components.select import ATTR_OPTION, ATTR_OPTIONS
from homeassistant.components.select import DOMAIN as SELECT_DOMAIN
from homeassistant.const import ATTR_ENTITY_ID, STATE_OFF, STATE_ON, STATE_PLAYING
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import entity_registry as er

from custom_components.skyq_control.aioskyq import Key, PowerState, SkyQConnectionError
from custom_components.skyq_control.const import DOMAIN

from .conftest import GATEWAY_HOST, MINI_HOST, MINI_SERIAL, FakeBox, make_entry, setup_entry

pytestmark = pytest.mark.usefixtures("mock_skyq")

PLAYER = "media_player.sky_q_gateway"
REMOTE = "remote.sky_q_gateway"
CHANNEL_SELECT = "select.sky_q_gateway_channel"
APP_SELECT = "select.sky_q_gateway_app"


@pytest.fixture(autouse=True)
def instant_refresh() -> None:
    """Don't wait the real 2 s before the post-command refresh."""
    with patch("custom_components.skyq_control.entity.REFRESH_AFTER_COMMAND", 0):
        yield


async def refresh(hass: HomeAssistant) -> None:
    entry = hass.config_entries.async_entries(DOMAIN)[0]
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()


# ------------------------------------------------------------------ media player state


async def test_live_tv_state(hass: HomeAssistant) -> None:
    await setup_entry(hass, make_entry())
    state = hass.states.get(PLAYER)
    assert state.state == STATE_PLAYING
    attrs = state.attributes
    assert attrs[ATTR_MEDIA_TITLE] == "Kingdom"
    assert attrs[ATTR_MEDIA_CHANNEL] == "101 BBC One SE HD"
    assert attrs[ATTR_MEDIA_CONTENT_TYPE] == MediaType.CHANNEL
    assert attrs[ATTR_MEDIA_CONTENT_ID] == "3008"
    assert attrs[ATTR_MEDIA_DURATION] == 1800
    assert attrs[ATTR_MEDIA_POSITION] == 0  # progress anchored at the programme start
    assert attrs[ATTR_MEDIA_POSITION_UPDATED_AT] is not None
    assert attrs[ATTR_INPUT_SOURCE] == "101 BBC One SE HD"
    assert attrs[ATTR_INPUT_SOURCE_LIST][:3] == ["101 BBC One SE HD", "102 BBC Two HD", "103 ITV1 HD"]
    assert attrs["next_programme"] == "News"
    assert attrs["channel_number"] == "101" and attrs["service_id"] == 3008
    assert attrs["entity_picture"]  # programme artwork


async def test_standby_app_and_recording_states(hass: HomeAssistant, boxes: dict[str, FakeBox]) -> None:
    await setup_entry(hass, make_entry())
    box = boxes[GATEWAY_HOST]

    box.power = PowerState.STANDBY
    await refresh(hass)
    assert hass.states.get(PLAYER).state == STATE_OFF
    assert hass.states.get(REMOTE).state == STATE_OFF

    box.power, box.uri = PowerState.ON, None
    box.push_app("YouTube")
    await refresh(hass)
    state = hass.states.get(PLAYER)
    assert state.state == STATE_ON
    assert state.attributes[ATTR_APP_NAME] == "YouTube"
    assert state.attributes[ATTR_MEDIA_CONTENT_TYPE] == MediaType.APP
    assert hass.states.get(APP_SELECT).state == "YouTube"

    box.push_app("com.bskyb.epgui")  # Sky's TV interface counts as watching TV, not an app
    box.uri = "file://pvr/2902aeb9"
    await refresh(hass)
    state = hass.states.get(PLAYER)
    assert state.state == STATE_PLAYING
    assert state.attributes[ATTR_MEDIA_TITLE] == "Recording P2902aeb9"
    assert state.attributes[ATTR_MEDIA_CONTENT_TYPE] in (MediaType.MOVIE, MediaType.EPISODE)
    assert state.attributes[ATTR_MEDIA_POSITION] == 600
    assert ATTR_APP_NAME not in state.attributes
    assert hass.states.get(APP_SELECT).state == "unknown"


async def test_unavailable_when_unreachable(hass: HomeAssistant, boxes: dict[str, FakeBox]) -> None:
    await setup_entry(hass, make_entry())
    boxes[GATEWAY_HOST].mock.get_transport_info = AsyncMock(side_effect=SkyQConnectionError("gone"))
    await refresh(hass)
    assert hass.states.get(PLAYER).state == "unavailable"


# ------------------------------------------------------------------ media player commands


@pytest.mark.parametrize(
    ("service", "method", "args"),
    [
        ("turn_on", "turn_on", ()),
        ("turn_off", "turn_off", ()),
        ("media_play", "play", ()),
        ("media_pause", "pause", ()),
        ("media_stop", "stop", ()),
        ("media_next_track", "press", (Key.CHANNEL_UP,)),
        ("media_previous_track", "press", (Key.CHANNEL_DOWN,)),
    ],
)
async def test_player_commands(
    hass: HomeAssistant, boxes: dict[str, FakeBox], service: str, method: str, args: tuple
) -> None:
    await setup_entry(hass, make_entry())
    box = boxes[GATEWAY_HOST].mock
    polls = box.get_media_info.await_count
    await hass.services.async_call(MP_DOMAIN, service, {ATTR_ENTITY_ID: PLAYER}, blocking=True)
    await hass.async_block_till_done()
    getattr(box, method).assert_awaited_once_with(*args)
    assert box.get_media_info.await_count > polls  # refreshed after the command


async def test_select_source_and_play_media(hass: HomeAssistant, boxes: dict[str, FakeBox]) -> None:
    await setup_entry(hass, make_entry())
    box = boxes[GATEWAY_HOST].mock
    await hass.services.async_call(
        MP_DOMAIN, "select_source", {ATTR_ENTITY_ID: PLAYER, ATTR_INPUT_SOURCE: "102 BBC Two HD"}, blocking=True
    )
    box.change_channel.assert_awaited_with("102")

    async def play(media_type: str, media_id: str) -> None:
        await hass.services.async_call(
            MP_DOMAIN,
            "play_media",
            {ATTR_ENTITY_ID: PLAYER, "media_content_type": media_type, "media_content_id": media_id},
            blocking=True,
        )

    await play("channel", "103")
    box.change_channel.assert_awaited_with("103")
    await play("channel", "sid:2075")
    box.tune.assert_awaited_with(2075)
    await play("app", "netflix")  # matched case-insensitively by title
    box.launch_app.assert_awaited_with("Netflix")
    await play("keys", "backup, 1,0,1")
    box.press.assert_awaited_with([Key.BACKUP, Key.DIGIT_1, Key.DIGIT_0, Key.DIGIT_1])

    with pytest.raises(ServiceValidationError, match="type"):
        await play("music", "x")
    with pytest.raises(ServiceValidationError):
        await play("channel", "BBC")
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            MP_DOMAIN, "select_source", {ATTR_ENTITY_ID: PLAYER, ATTR_INPUT_SOURCE: "999 Nope"}, blocking=True
        )


async def test_command_failure_is_reported(hass: HomeAssistant, boxes: dict[str, FakeBox]) -> None:
    await setup_entry(hass, make_entry())
    boxes[GATEWAY_HOST].mock.play = AsyncMock(side_effect=SkyQConnectionError("remote port closed"))
    with pytest.raises(HomeAssistantError, match="remote port closed"):
        await hass.services.async_call(MP_DOMAIN, "media_play", {ATTR_ENTITY_ID: PLAYER}, blocking=True)


async def test_sources_are_every_channel(hass: HomeAssistant) -> None:
    """Every channel, not just favourites: TV by number, then radio (numbers like 0101)."""
    await setup_entry(hass, make_entry(MINI_HOST, MINI_SERIAL))
    sources = hass.states.get(f"media_player.sky_q_mini_{MINI_HOST.replace('.', '_')}").attributes[
        ATTR_INPUT_SOURCE_LIST
    ]
    assert len(sources) == 377 and sources[0] == "101 BBC One SE HD"
    tv, radio = sources[:-42], sources[-42:]  # the fixture has 42 radio services
    tv_numbers = [int(s.split(" ", 1)[0]) for s in tv]
    assert tv_numbers == sorted(tv_numbers) and max(tv_numbers) < 996
    assert radio[0].startswith("0101 ") and "996 Chl Line-up" in radio


# ------------------------------------------------------------------ actions


async def test_actions(hass: HomeAssistant, boxes: dict[str, FakeBox]) -> None:
    await setup_entry(hass, make_entry())
    box = boxes[GATEWAY_HOST].mock
    await hass.services.async_call(
        DOMAIN, "send_keys", {ATTR_ENTITY_ID: PLAYER, "keys": ["tv_guide", "down", "select"]}, blocking=True
    )
    box.press.assert_awaited_with([Key.TV_GUIDE, Key.DOWN, Key.SELECT])

    box.press.reset_mock()
    with patch("custom_components.skyq_control.media_player.asyncio.sleep") as sleep:
        await hass.services.async_call(
            DOMAIN, "send_keys", {ATTR_ENTITY_ID: PLAYER, "keys": "up, up", "delay": 1.5}, blocking=True
        )
    assert box.press.await_count == 2 and call(1.5) in sleep.await_args_list

    await hass.services.async_call(DOMAIN, "change_channel", {ATTR_ENTITY_ID: PLAYER, "channel": "401"}, blocking=True)
    box.change_channel.assert_awaited_with("401")
    await hass.services.async_call(DOMAIN, "launch_app", {ATTR_ENTITY_ID: PLAYER, "app": "Disney+"}, blocking=True)
    box.launch_app.assert_awaited_with("DisneyPlus")

    with pytest.raises(ServiceValidationError, match="Unknown"):
        await hass.services.async_call(DOMAIN, "send_keys", {ATTR_ENTITY_ID: PLAYER, "keys": ["warp"]}, blocking=True)


# ------------------------------------------------------------------ remote, buttons, selects


async def test_remote(hass: HomeAssistant, boxes: dict[str, FakeBox]) -> None:
    await setup_entry(hass, make_entry())
    box = boxes[GATEWAY_HOST].mock
    state = hass.states.get(REMOTE)
    assert state.state == STATE_ON and "red_button" in state.attributes["commands"]
    with patch("custom_components.skyq_control.remote.asyncio.sleep") as sleep:
        await hass.services.async_call(
            REMOTE_DOMAIN,
            "send_command",
            {ATTR_ENTITY_ID: REMOTE, ATTR_COMMAND: ["channelup", "7"], ATTR_NUM_REPEATS: 2, ATTR_DELAY_SECS: 0.3},
            blocking=True,
        )
    assert box.press.await_count == 2 and box.press.await_args.args == ([Key.CHANNEL_UP, Key.DIGIT_7],)
    assert sleep.await_args_list.count(call(0.3)) == 1  # between the two repeats only
    await hass.services.async_call(REMOTE_DOMAIN, "turn_off", {ATTR_ENTITY_ID: REMOTE}, blocking=True)
    box.turn_off.assert_awaited_once()
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            REMOTE_DOMAIN, "send_command", {ATTR_ENTITY_ID: REMOTE, ATTR_COMMAND: ["nope"]}, blocking=True
        )


async def test_buttons(hass: HomeAssistant, boxes: dict[str, FakeBox], entity_registry: er.EntityRegistry) -> None:
    entry = await setup_entry(hass, make_entry())
    buttons = [e for e in er.async_entries_for_config_entry(entity_registry, entry.entry_id) if e.domain == "button"]
    assert len(buttons) == len(Key)
    assert not any(e.disabled for e in buttons)
    await hass.services.async_call(
        "button", "press", {ATTR_ENTITY_ID: "button.sky_q_gateway_red_button"}, blocking=True
    )
    boxes[GATEWAY_HOST].mock.press.assert_awaited_with(Key.RED_BUTTON)


async def test_selects(hass: HomeAssistant, boxes: dict[str, FakeBox]) -> None:
    await setup_entry(hass, make_entry())
    box = boxes[GATEWAY_HOST].mock
    channel = hass.states.get(CHANNEL_SELECT)
    assert channel.state == "101 BBC One SE HD" and len(channel.attributes[ATTR_OPTIONS]) == 377
    apps = hass.states.get(APP_SELECT)
    assert apps.attributes[ATTR_OPTIONS] == ["Apple TV", "Disney+", "Netflix", "Services", "YouTube"]  # no EPG_UI

    await hass.services.async_call(
        SELECT_DOMAIN, "select_option", {ATTR_ENTITY_ID: CHANNEL_SELECT, ATTR_OPTION: "103 ITV1 HD"}, blocking=True
    )
    box.change_channel.assert_awaited_with("103")
    await hass.services.async_call(
        SELECT_DOMAIN, "select_option", {ATTR_ENTITY_ID: APP_SELECT, ATTR_OPTION: "Apple TV"}, blocking=True
    )
    box.launch_app.assert_awaited_with("apple.tv")
