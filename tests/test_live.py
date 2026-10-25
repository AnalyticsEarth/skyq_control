"""The integration against real boxes, with the real aioskyq (no mocks). Read-only.

SKYQ_GATEWAY=10.0.0.51 SKYQ_MINIS=10.0.0.65,10.0.0.93 uv run pytest tests/test_live.py -m live
"""

from __future__ import annotations

import asyncio
import os
from unittest.mock import patch

import pytest
import pytest_socket
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.diagnostics import get_diagnostics_for_config_entry
from pytest_homeassistant_custom_component.typing import ClientSessionGenerator, WebSocketGenerator

from custom_components.skyq_control.aioskyq import Key, PowerState
from custom_components.skyq_control.const import DOMAIN

GATEWAY = os.environ.get("SKYQ_GATEWAY", "")
MINIS = [h.strip() for h in os.environ.get("SKYQ_MINIS", "").split(",") if h.strip()]

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(not GATEWAY, reason="set SKYQ_GATEWAY (and optionally SKYQ_MINIS)"),
]


@pytest.fixture
def allow_boxes(socket_enabled: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """Open sockets (and DNS, for Sky's guide hosts) for this test only."""
    import socket

    from pytest_homeassistant_custom_component import plugins

    monkeypatch.setattr(socket, "getaddrinfo", plugins._real_getaddrinfo)
    for name in ("gethostbyname", "gethostbyname_ex"):
        if real := getattr(plugins, f"_real_{name}", None):
            monkeypatch.setattr(socket, name, real)
    pytest_socket.socket_allow_hosts(
        [GATEWAY, *MINIS, "127.0.0.1", "awk.epgsky.com", "atlantis.epgsky.com"], allow_unix_socket=True
    )


async def test_live_household(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    hass_ws_client: WebSocketGenerator,
    device_registry: dr.DeviceRegistry,
    allow_boxes: None,
) -> None:
    import aiohttp
    from homeassistant.setup import async_setup_component

    from custom_components.skyq_control.aioskyq import SkyQBox

    # The harness's shared HA session uses a DNS resolver bound to another loop; use a plain one,
    # as a real Home Assistant would.
    session = aiohttp.ClientSession()
    patcher = patch("custom_components.skyq_control.async_get_clientsession", return_value=session)
    patcher.start()
    entries = []
    for host in [GATEWAY, *MINIS]:
        async with SkyQBox(host) as probe:  # the same unique id the config flow would store
            unique_id = (await probe.get_device_info()).unique_id
        entry = MockConfigEntry(domain=DOMAIN, unique_id=unique_id, data={CONF_HOST: host}, title=host)
        entry.add_to_hass(hass)
        entries.append(entry)
    assert await async_setup_component(hass, DOMAIN, {})  # sets up every entry
    assert await async_setup_component(hass, "diagnostics", {})  # before the websocket client freezes the router
    await hass.async_block_till_done()

    try:
        gateway, *minis = entries
        for entry in entries:
            assert entry.state is ConfigEntryState.LOADED, entry.title
            data = entry.runtime_data.data
            assert data.power in (PowerState.ON, PowerState.STANDBY)
            assert len(data.channels) > 100 and data.apps
            if data.media and data.media.sid and data.channel and not data.channel.adult:
                assert data.programme is not None, f"no guide data on {entry.title} ({data.channel.name})"
            print(
                f"\n{entry.title:12} {data.device.hardware_model} {data.power.value:7}"
                f" channel={data.channel.name if data.channel else '-'}"
                f" programme={data.programme.title if data.programme else '-'}"
                f" app={data.visible_app or '-'} fetches_pvr={entry.runtime_data.fetches_pvr}"
            )

        assert gateway.runtime_data.fetches_pvr and gateway.runtime_data.data.recordings
        assert not any(m.runtime_data.fetches_pvr for m in minis)  # recordings fetched once per household

        gateway_device = device_registry.async_get_device_by_identifier(
            (DOMAIN, gateway.unique_id or ""), gateway.entry_id
        )
        assert gateway_device and gateway_device.model == "Sky Q Gateway"
        for mini in minis:
            device = device_registry.async_get_device_by_identifier((DOMAIN, mini.unique_id or ""), mini.entry_id)
            assert device and device.via_device_id == gateway_device.id

        async with asyncio.timeout(20):  # push streams connect and deliver their snapshots
            while not all(
                e.runtime_data.data.system_status and all(e.runtime_data.box.streams_connected.values())
                for e in entries
            ):
                await asyncio.sleep(0.2)

        from homeassistant.helpers import entity_registry as er

        registry = er.async_get(hass)
        for entry in entries:
            entities = {e.domain: e for e in er.async_entries_for_config_entry(registry, entry.entry_id)}
            player = hass.states.get(entities["media_player"].entity_id)
            assert player is not None and player.state not in ("unavailable", "unknown"), entry.title
            assert hass.states.get(entities["remote"].entity_id).state in ("on", "off")
            options = hass.states.get(
                f"{entities['media_player'].entity_id.replace('media_player.', 'select.')}_channel"
            )
            assert options is not None and options.attributes["options"]
            print(
                f"{entry.title:12} {player.entity_id}: {player.state}"
                f" | {player.attributes.get('media_channel', '-')} | {player.attributes.get('media_title', '-')}"
                f" | app={player.attributes.get('app_name', '-')}"
                f" | sources={len(player.attributes.get('source_list', []))}"
            )

        # Every key button is enabled.
        buttons = [e for e in er.async_entries_for_config_entry(registry, gateway.entry_id) if e.domain == "button"]
        assert len(buttons) == len(Key) and not any(e.disabled for e in buttons)

        # The guide: the shared client, the media browser and get_guide.
        player_id = next(
            e.entity_id
            for e in er.async_entries_for_config_entry(registry, gateway.entry_id)
            if e.domain == "media_player"
        )
        client = await hass_ws_client(hass)
        started = asyncio.get_running_loop().time()
        await client.send_json_auto_id(
            {
                "type": "media_player/browse_media",
                "entity_id": player_id,
                "media_content_id": "genre:12",
                "media_content_type": "genre:12",
            }
        )
        response = await client.receive_json()
        assert response["success"], response
        channels = response["result"]["children"]
        with_titles = [c for c in channels if " · " in c["title"]]
        print(
            f"browse Entertainment: {len(channels)} channels, {len(with_titles)} with what's on,"
            f" {asyncio.get_running_loop().time() - started:.1f}s"
        )
        assert len(with_titles) > len(channels) / 2
        await client.send_json_auto_id(
            {
                "type": "media_player/browse_media",
                "entity_id": player_id,
                "media_content_id": channels[0]["media_content_id"],
                "media_content_type": "channel",
            }
        )
        schedule = (await client.receive_json())["result"]["children"]
        print(f"browse {channels[0]['title']}: {[p['title'] for p in schedule[:4]]}")
        assert schedule and schedule[0]["can_play"]

        response = await hass.services.async_call(
            DOMAIN,
            "get_guide",
            {"entity_id": player_id, "channels": ["101", "401"], "hours": 3},
            blocking=True,
            return_response=True,
        )
        programmes = response[player_id]["programmes"]
        print("get_guide 101/401:", [(p["channel_number"], p["start"][11:16], p["title"]) for p in programmes][:8])
        assert {p["channel_number"] for p in programmes} == {"101", "401"}
        assert all(p["event_id"].startswith("E") for p in programmes)

        started = asyncio.get_running_loop().time()
        response = await hass.services.async_call(
            DOMAIN,
            "get_guide",
            {"entity_id": player_id, "search": "news", "hours": 1},
            blocking=True,
            return_response=True,
        )
        found = response[player_id]["programmes"]
        print(f"search 'news': {len(found)} in {asyncio.get_running_loop().time() - started:.1f}s")
        assert found
        from custom_components.skyq_control import GUIDE_CLIENTS

        assert len(hass.data[GUIDE_CLIENTS]) == 1  # every box shares one guide cache

        diagnostics = await get_diagnostics_for_config_entry(hass, hass_client, gateway)
        assert diagnostics["device"]["serialNumber"] == "**REDACTED**"
        assert diagnostics["system_status"]["smartcard"]["viewingCardNumber"] == "**REDACTED**"
        assert diagnostics["coordinator"]["streams_connected"]["system/status"] is True
    finally:
        for entry in entries:
            await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
        patcher.stop()
        await session.close()
