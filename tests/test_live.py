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
from pytest_homeassistant_custom_component.typing import ClientSessionGenerator

from custom_components.skyq_control.aioskyq import PowerState
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
    hass: HomeAssistant, hass_client: ClientSessionGenerator, device_registry: dr.DeviceRegistry, allow_boxes: None
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
