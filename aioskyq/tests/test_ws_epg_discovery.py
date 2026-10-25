from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, timedelta
from typing import Any

import aiohttp
import pytest

from aioskyq import Channel, EpgClient, SkyQConnectionError
from aioskyq.discovery import _SsdpProtocol
from aioskyq.ws import StatusStream

from .fakebox import FakeSkyQ, load

FIXTURE_DAY = date(2026, 10, 25)


async def wait_for(predicate: Any, timeout: float = 3.0) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.02)


# ------------------------------------------------------------------ websockets


async def test_stream_snapshot_push_and_reconnect(gateway: FakeSkyQ, session: aiohttp.ClientSession) -> None:
    messages: list[dict[str, Any]] = []
    states: list[bool] = []
    stream = StatusStream(
        session,
        "127.0.0.1",
        "system/status",
        messages.append,
        port=gateway.rest_port,
        on_connection_change=states.append,
    )
    stream.start()
    await wait_for(lambda: len(messages) == 1)
    assert stream.connected and "entitlements" in messages[0]

    await gateway.push("system/status", {"systemstate": {"state": "standby"}})
    await wait_for(lambda: len(messages) == 2)

    await gateway.drop_sockets("system/status")  # box closes the socket -> reconnect + fresh snapshot
    await wait_for(lambda: len(messages) == 3, timeout=5)
    assert states[:2] == [True, False] and stream.connected
    await stream.stop()
    assert not stream.connected and not stream.running


async def test_stream_periodic_refresh(gateway: FakeSkyQ, session: aiohttp.ClientSession) -> None:
    messages: list[dict[str, Any]] = []
    stream = StatusStream(
        session, "127.0.0.1", "pvr/status", messages.append, port=gateway.rest_port, refresh_interval=0.2
    )
    stream.start()
    await wait_for(lambda: len(messages) >= 3)  # each forced reconnect delivers a new snapshot
    await stream.stop()


async def test_stream_retries_when_unreachable(session: aiohttp.ClientSession) -> None:
    stream = StatusStream(session, "127.0.0.1", "system/status", lambda _: None, port=1)
    stream.start()
    await asyncio.sleep(0.1)
    assert stream.running and not stream.connected
    await stream.stop()


async def test_stream_bad_callback_and_payloads(gateway: FakeSkyQ, session: aiohttp.ClientSession) -> None:
    seen: list[dict[str, Any]] = []

    async def callback(data: dict[str, Any]) -> None:
        seen.append(data)
        raise RuntimeError("listener bug")

    stream = StatusStream(session, "127.0.0.1", "apps/status", callback, port=gateway.rest_port)
    stream.start()
    await wait_for(lambda: len(seen) == 1)
    for ws in gateway._sockets["apps/status"]:
        await ws.send_str("not json")
        await ws.send_str("[1, 2]")
    await gateway.push("apps/status", {"apps": []})
    await wait_for(lambda: len(seen) == 2)  # junk ignored, the stream survived the listener error
    await stream.stop()


# ------------------------------------------------------------------ EPG


def epg(gateway: FakeSkyQ, session: aiohttp.ClientSession) -> EpgClient:
    return EpgClient(session, hawk_base=gateway.hawk_base, atlantis_base=gateway.atlantis_base)


async def test_epg_batches_and_uhd(gateway: FakeSkyQ, session: aiohttp.ClientSession) -> None:
    client = epg(gateway, session)
    channels = [Channel.from_api(s) for s in load("rest/services_bouquet.json")["services"]][:45]
    uhd = Channel.from_api({"sid": "7201", "c": "401", "t": "SkySpME UHD", "sf": "uhd"})
    result = await client.get_day([*channels, uhd], FIXTURE_DAY)
    hawk_calls = [r for r in gateway.epg_requests if r.startswith("hawk")]
    assert len(hawk_calls) == 3  # 45 SIDs in batches of 20
    assert max(len(r.split(" ", 1)[1].split(",")) for r in hawk_calls) == 20
    assert gateway.epg_requests[-1] == "atlantis 7201"
    assert len(result[3008]) == 30 and len(result[7201]) == 16
    assert all(e.sid == 3008 for e in result[3008])

    before = len(gateway.epg_requests)
    await client.get_day([3008, uhd], FIXTURE_DAY)
    assert len(gateway.epg_requests) == before  # served from cache
    client.clear_cache()
    await client.get_day([3008], FIXTURE_DAY)
    assert len(gateway.epg_requests) == before + 1


async def test_epg_now_next_and_find(gateway: FakeSkyQ, session: aiohttp.ClientSession) -> None:
    client = epg(gateway, session)
    events = (await client.get_day([3008], FIXTURE_DAY))[3008]
    when = events[5].start + timedelta(minutes=1)
    current, upcoming = await client.now_and_next(3008, when)
    assert current == events[5] and upcoming == events[6]
    window = await client.get_events(3008, events[2].start, events[4].end)
    assert [e.eid for e in window] == [e.eid for e in events[2:5]]
    assert await client.find_event(3008, "nope", days=0) is None


async def test_epg_errors(session: aiohttp.ClientSession) -> None:
    client = EpgClient(session, hawk_base="http://127.0.0.1:1/hawk")
    with pytest.raises(SkyQConnectionError):
        await client.get_day([3008], FIXTURE_DAY)


async def test_epg_atlantis_needs_headers(gateway: FakeSkyQ, session: aiohttp.ClientSession) -> None:
    async with session.get(f"{gateway.atlantis_base}/schedule/20261025/7201") as resp:
        assert resp.status == 400  # what broke pyskyqremote's EPG


# ------------------------------------------------------------------ discovery


def test_ssdp_filters_non_sky_devices() -> None:
    protocol = _SsdpProtocol()
    protocol.datagram_received(
        b"HTTP/1.1 200 OK\r\nST: upnp:rootdevice\r\nSERVER: Hue/1.0\r\n\r\n", ("10.0.0.11", 1900)
    )
    protocol.datagram_received(
        b"HTTP/1.1 200 OK\r\nST: urn:schemas-nds-com:device:MRSkyControl:2\r\n"
        b"LOCATION: http://10.0.0.65:49153/description0.xml\r\n\r\n",
        ("10.0.0.65", 1900),
    )
    protocol.datagram_received(
        b"HTTP/1.1 200 OK\r\nST: urn:schemas-nds-com:device:GatewaySkyControl:2\r\n\r\n", ("10.0.0.51", 1900)
    )
    assert set(protocol.found) == {"10.0.0.65", "10.0.0.51"}
    assert protocol.found["10.0.0.51"].is_gateway and not protocol.found["10.0.0.65"].is_gateway
    assert protocol.found["10.0.0.65"].location.endswith("description0.xml")


def test_fixture_day_matches() -> None:
    assert load("epg/hawk_schedule.json")["date"] == FIXTURE_DAY.strftime("%Y%m%d")
    assert datetime.now(UTC).year >= 2026
