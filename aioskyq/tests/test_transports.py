"""REST, SOAP and remote-key transports against the fake box."""

from __future__ import annotations

import asyncio

import aiohttp
import pytest

from aioskyq import (
    Key,
    SkyQApiError,
    SkyQConnectionError,
    SkyQNotFoundError,
    SkyQNotSupportedError,
    SkyQSoapError,
    SkyQTransportLockedError,
)
from aioskyq.remote import RemoteClient, key_packet, resolve_key
from aioskyq.rest import RestClient
from aioskyq.soap import SoapClient, parse_description, parse_soap_response

from .fakebox import FIXTURES, FakeSkyQ
from .test_ws_epg_discovery import wait_for

# ------------------------------------------------------------------ REST


async def test_rest_errors(gateway: FakeSkyQ, session: aiohttp.ClientSession) -> None:
    rest = RestClient(session, "127.0.0.1", gateway.rest_port)
    assert (await rest.get("system/time"))["utc"]
    with pytest.raises(SkyQNotSupportedError) as not_supported:
        await rest.get("schedule/20261025/3008")
    assert not_supported.value.code == 104
    with pytest.raises(SkyQNotFoundError) as invalid:
        await rest.get("nope")
    assert invalid.value.code == 101 and invalid.value.status == 404
    with pytest.raises(SkyQApiError) as server_error:
        await rest.post("pvr/action/setlastplayedposition", {"pos": 0, "pvrid": "P2902aeb9"})
    assert server_error.value.status == 500
    assert await rest.post("system/action/ping") is None


async def test_rest_unreachable(session: aiohttp.ClientSession) -> None:
    rest = RestClient(session, "127.0.0.1", 1)
    with pytest.raises(SkyQConnectionError):
        await rest.get("system/time")


# ------------------------------------------------------------------ SOAP


def test_parse_gateway_descriptions() -> None:
    devices = [
        d
        for p in sorted((FIXTURES / "gateway/upnp").glob("description*.xml"))
        for d in parse_description(p.read_text(), int(p.stem[-1]))
    ]
    types = {d.device_type.rsplit(":", 2)[-2] for d in devices}
    assert {"GatewaySkyControl", "GatewaySkyServe"} <= types
    play = [s for d in devices for s in d.services if s.service_id.endswith("SkyPlay")]
    assert len(play) == 2  # SkyControl copy + the broken SkyServe copy


def test_parse_soap_faults() -> None:
    with pytest.raises(SkyQTransportLockedError):
        parse_soap_response(
            "<Envelope><Body><Fault><faultstring>UPnPError</faultstring><detail><UPnPError>"
            "<errorCode>705</errorCode><errorDescription>TRANSPORT IS LOCKED</errorDescription>"
            "</UPnPError></detail></Fault></Body></Envelope>",
            "Pause",
        )
    with pytest.raises(SkyQSoapError) as other:
        parse_soap_response(
            "<Envelope><Body><Fault><detail><UPnPError><errorCode>401</errorCode>"
            "</UPnPError></detail></Fault></Body></Envelope>",
            "X",
        )
    assert other.value.code == 401 and not isinstance(other.value, SkyQTransportLockedError)
    assert parse_soap_response("<Envelope><Body/></Envelope>", "Play") == {}


async def test_soap_picks_working_play_service(fake: FakeSkyQ, session: aiohttp.ClientSession) -> None:
    soap = SoapClient(session, "127.0.0.1", fake.upnp_port)
    service = await soap.play_service()
    assert "SkyControl" in service.device_type
    assert (await soap.get_transport_info()).state_raw == "PLAYING"
    assert (await soap.get_media_info()).sid == 3008
    assert (await soap.get_position_info()).position == 0
    assert "Pause" in await soap.get_transport_actions()
    with pytest.raises(SkyQTransportLockedError):
        await soap.call_play("Pause")


async def test_soap_no_descriptions(session: aiohttp.ClientSession) -> None:
    with pytest.raises(SkyQConnectionError):
        await SoapClient(session, "127.0.0.1", 1).play_service()


# ------------------------------------------------------------------ remote keys


def test_key_resolution() -> None:
    assert resolve_key("channelup") is Key.CHANNEL_UP
    assert resolve_key("CHANNEL_UP") is Key.CHANNEL_UP
    assert resolve_key("7") is Key.DIGIT_7
    assert resolve_key("redbutton") is Key.RED_BUTTON
    assert resolve_key(241) is Key.SKY
    with pytest.raises(ValueError):
        resolve_key("teleport")
    assert key_packet(Key.SKY) == bytearray([4, 1, 0, 0, 0, 0, 239, 1])


async def test_remote_press_order(gateway: FakeSkyQ) -> None:
    remote = RemoteClient("127.0.0.1", gateway.remote_port, key_gap=0.01)
    assert await remote.check()
    await asyncio.gather(remote.press("1", "0", "1"), remote.press(Key.SELECT))
    await wait_for(lambda: len(gateway.keys) == 4)
    assert gateway.keys in ([49, 48, 49, 1], [1, 49, 48, 49])  # whole sequences never interleave
    await remote.press(["up", "down"])
    await wait_for(lambda: len(gateway.keys) == 6)
    assert gateway.keys[-2:] == [16, 17]


async def test_remote_unreachable() -> None:
    remote = RemoteClient("127.0.0.1", 1, key_gap=0.01)
    assert not await remote.check()
    with pytest.raises(SkyQConnectionError):
        await remote.press(Key.SELECT)
