"""Config, discovery, reconfigure and options flows."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.config_entries import SOURCE_SSDP, SOURCE_USER
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers.service_info.ssdp import SsdpServiceInfo

from custom_components.skyq_control.aioskyq import DiscoveredBox, SkyQUnsupportedDeviceError
from custom_components.skyq_control.const import CONF_KEY_GAP, CONF_SCAN_INTERVAL, DOMAIN, MANUAL_ENTRY

from .conftest import GATEWAY_HOST, GATEWAY_SERIAL, MINI_HOST, MINI_SERIAL, FakeBox, make_entry, setup_entry

pytestmark = pytest.mark.usefixtures("mock_skyq")

GATEWAY_ST = "urn:schemas-nds-com:device:GatewaySkyControl:2"
MINI_ST = "urn:schemas-nds-com:device:MRSkyControl:2"


def ssdp(host: str, st: str = MINI_ST) -> SsdpServiceInfo:
    return SsdpServiceInfo(
        ssdp_usn=f"uuid:x::{st}",
        ssdp_st=st,
        ssdp_location=f"http://{host}:49153/description0.xml",
        upnp={"deviceType": st, "manufacturer": "Sky"},
    )


def patch_discover(*found: DiscoveredBox):  # type: ignore[no-untyped-def]
    return patch("custom_components.skyq_control.config_flow.discover", AsyncMock(return_value=list(found)))


# ------------------------------------------------------------------ user


async def test_user_picks_discovered_gateway(hass: HomeAssistant) -> None:
    with patch_discover(
        DiscoveredBox(GATEWAY_HOST, GATEWAY_ST, None, None), DiscoveredBox(MINI_HOST, MINI_ST, None, None)
    ):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "user"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_HOST: GATEWAY_HOST})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Sky Q Gateway"
    assert result["data"] == {CONF_HOST: GATEWAY_HOST}
    assert result["result"].unique_id == GATEWAY_SERIAL


async def test_user_hides_configured_boxes_and_falls_back_to_manual(hass: HomeAssistant) -> None:
    make_entry().add_to_hass(hass)
    with patch_discover(DiscoveredBox(GATEWAY_HOST, GATEWAY_ST, None, None)):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["step_id"] == "manual"  # the only box found is already set up


async def test_user_manual_choice(hass: HomeAssistant) -> None:
    with patch_discover(DiscoveredBox(MINI_HOST, MINI_ST, None, None)):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_HOST: MANUAL_ENTRY})
    assert result["step_id"] == "manual"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_HOST: f" {MINI_HOST} "})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == f"Sky Q Mini ({MINI_HOST})"
    assert result["result"].unique_id == MINI_SERIAL


async def test_manual_cannot_connect_then_recovers(hass: HomeAssistant) -> None:
    with patch_discover():
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_HOST: "192.0.2.99"})
    assert result["type"] is FlowResultType.FORM and result["errors"] == {"base": "cannot_connect"}
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_HOST: GATEWAY_HOST})
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_discovery_os_error_goes_manual(hass: HomeAssistant) -> None:
    with patch("custom_components.skyq_control.config_flow.discover", AsyncMock(side_effect=OSError("no multicast"))):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["step_id"] == "manual"


async def test_manual_unknown_error(hass: HomeAssistant, boxes: dict[str, FakeBox]) -> None:
    boxes[GATEWAY_HOST].mock.get_device_info.side_effect = RuntimeError("boom")
    with patch_discover():
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_HOST: GATEWAY_HOST})
    assert result["errors"] == {"base": "unknown"}


async def test_unsupported_device(hass: HomeAssistant, boxes: dict[str, FakeBox]) -> None:
    boxes[GATEWAY_HOST].mock.get_device_info.side_effect = SkyQUnsupportedDeviceError("Sky Glass")
    with patch_discover():
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_HOST: GATEWAY_HOST})
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "unsupported_device"


async def test_no_unique_id(hass: HomeAssistant, boxes: dict[str, FakeBox]) -> None:
    from dataclasses import replace

    info = await boxes[GATEWAY_HOST].mock.get_device_info()
    boxes[GATEWAY_HOST].mock.get_device_info = AsyncMock(
        return_value=replace(info, serial_number=None, mac_address=None)
    )
    with patch_discover():
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_HOST: GATEWAY_HOST})
    assert result["reason"] == "no_unique_id"


async def test_manual_same_box_new_ip_updates_entry(hass: HomeAssistant, boxes: dict[str, FakeBox]) -> None:
    entry = make_entry(host="192.0.2.50")
    entry.add_to_hass(hass)
    with patch_discover():
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_HOST: GATEWAY_HOST})
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "already_configured"
    assert entry.data[CONF_HOST] == GATEWAY_HOST


# ------------------------------------------------------------------ SSDP


async def test_ssdp_confirm_creates_entry(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_SSDP}, data=ssdp(MINI_HOST))
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "confirm"
    assert result["description_placeholders"] == {
        "name": f"Sky Q Mini ({MINI_HOST})",
        "model": "EM150",
        "host": MINI_HOST,
    }
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY and result["result"].unique_id == MINI_SERIAL


async def test_ssdp_already_configured_host(hass: HomeAssistant) -> None:
    make_entry(MINI_HOST, MINI_SERIAL).add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_SSDP}, data=ssdp(MINI_HOST))
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "already_configured"


async def test_ssdp_box_moved_ip(hass: HomeAssistant) -> None:
    entry = make_entry("192.0.2.77", MINI_SERIAL)
    entry.add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_SSDP}, data=ssdp(MINI_HOST))
    assert result["reason"] == "already_configured"
    assert entry.data[CONF_HOST] == MINI_HOST


@pytest.mark.parametrize(
    ("location", "reason"), [("http://192.0.2.99:49153/description0.xml", "cannot_connect"), (None, "cannot_connect")]
)
async def test_ssdp_aborts(hass: HomeAssistant, location: str | None, reason: str) -> None:
    info = ssdp("192.0.2.99")
    info.ssdp_location = location
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_SSDP}, data=info)
    assert result["type"] is FlowResultType.ABORT and result["reason"] == reason


async def test_ssdp_unsupported(hass: HomeAssistant, boxes: dict[str, FakeBox]) -> None:
    boxes[MINI_HOST].mock.get_device_info.side_effect = SkyQUnsupportedDeviceError("TV")
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_SSDP}, data=ssdp(MINI_HOST))
    assert result["reason"] == "unsupported_device"


# ------------------------------------------------------------------ reconfigure & options


async def test_reconfigure(hass: HomeAssistant, boxes: dict[str, FakeBox]) -> None:
    boxes["192.0.2.60"] = FakeBox("gateway", "192.0.2.60", GATEWAY_SERIAL)  # same box, new address
    entry = await setup_entry(hass, make_entry())
    result = await entry.start_reconfigure_flow(hass)
    assert result["step_id"] == "reconfigure"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_HOST: "192.0.2.99"})
    assert result["errors"] == {"base": "cannot_connect"}
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_HOST: MINI_HOST})
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "wrong_device"
    result = await entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_HOST: "192.0.2.60"})
    assert result["reason"] == "reconfigure_successful"
    assert entry.data[CONF_HOST] == "192.0.2.60"


async def test_reconfigure_unsupported(hass: HomeAssistant, boxes: dict[str, FakeBox]) -> None:
    entry = await setup_entry(hass, make_entry())
    boxes[GATEWAY_HOST].mock.get_device_info = AsyncMock(side_effect=SkyQUnsupportedDeviceError("TV"))
    result = await entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_HOST: GATEWAY_HOST})
    assert result["reason"] == "unsupported_device"


async def test_options_flow_reloads(hass: HomeAssistant, mock_skyq: MagicMock) -> None:
    entry = await setup_entry(hass, make_entry())
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["step_id"] == "init"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_KEY_GAP: 0.8, CONF_SCAN_INTERVAL: 10}
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options == {CONF_KEY_GAP: 0.8, CONF_SCAN_INTERVAL: 10}
    assert mock_skyq.call_args.kwargs["key_gap"] == 0.8  # reloaded with the new spacing
    assert entry.runtime_data.update_interval.total_seconds() == 10
