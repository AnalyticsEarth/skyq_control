"""SkyQBox end to end against the fake gateway and Mini."""

from __future__ import annotations

import asyncio
from datetime import date, timedelta
from typing import Any
from unittest.mock import patch

import aiohttp
import pytest

from aioskyq import (
    AppsStatus,
    EpgClient,
    Key,
    PowerState,
    PvrStatus,
    RecordingStatus,
    SkyQBox,
    SkyQNotFoundError,
    SkyQUnsupportedDeviceError,
    SystemStatus,
)

from .conftest import make_client
from .fakebox import FakeSkyQ, load
from .test_ws_epg_discovery import FIXTURE_DAY, wait_for


async def test_device_and_lists(client: SkyQBox, fake: FakeSkyQ) -> None:
    info = await client.get_device_info()
    assert info.is_gateway is (fake.kind == "gateway")
    assert await client.get_device_info() is info  # cached
    assert (await client.get_system_time()).utc is not None
    assert await client.get_power_state() is PowerState.ON
    assert await client.ping()

    channels = await client.get_channels()
    assert len(channels) == 377
    assert ("GET", "services/4101/1", {}) in fake.requests  # the box's own bouquet
    assert (await client.get_channel_by_number(101)).sid == 3008
    assert (await client.get_channel(3008)).name.startswith("BBC One")
    details = await client.get_channel_details(3008)
    assert details.description
    # Real data: only the gateway streams, so Minis report no profiles and isbroadcasting=false.
    assert details.is_broadcasting is bool(details.streaming_profiles) is (fake.kind == "gateway")

    favourites = await client.get_favourites()  # per box: the Mini has none
    assert [f.position for f in favourites] == sorted(f.position for f in favourites)
    assert bool(favourites) is (fake.kind == "gateway")
    assert any(g.title == "Kids" for g in await client.get_genres())
    assert any(a.app_id == "YouTube" for a in await client.get_apps())
    assert (await client.get_pin_status()).tries_left == 3
    assert (await client.get_storage()).used_percent > 0


async def test_power_state_standby_and_off(gw_client: SkyQBox, gateway: FakeSkyQ) -> None:
    gateway.standby = True
    assert await gw_client.get_power_state() is PowerState.STANDBY
    await gw_client.turn_on()
    await wait_for(lambda: gateway.keys == [Key.POWER])
    assert not gateway.standby
    await gw_client.turn_on()  # already on: no key sent
    await asyncio.sleep(0.05)
    assert gateway.keys == [Key.POWER]
    await gw_client.turn_off()
    await wait_for(lambda: gateway.standby)
    await gateway.stop()
    assert await gw_client.get_power_state() is PowerState.OFF


async def test_unsupported_device(gateway: FakeSkyQ, session: aiohttp.ClientSession) -> None:
    client = make_client(gateway, session)
    original = load("gateway/rest/system_information.json")
    with (
        patch(
            "tests.fakebox.load",
            side_effect=lambda n: (
                {**original, "deviceType": "IPSETTOPBOX"} if n.endswith("system_information.json") else load(n)
            ),
        ),
        pytest.raises(SkyQUnsupportedDeviceError),
    ):
        await client.get_device_info()


async def test_recordings_paging_and_details(gw_client: SkyQBox, gateway: FakeSkyQ) -> None:
    gateway.page_size_cap = 3
    listing = await gw_client.get_recordings()
    assert len(listing.recordings) == 7 and listing.total == 7
    assert sum(1 for r in gateway.requests if r[1] == "pvr") == 3
    recording = await gw_client.get_recording("P2902aeb9")
    assert recording.title == "Recording P2902aeb9"
    with pytest.raises(SkyQNotFoundError):
        await gw_client.get_recording("Pnope")
    changes = await gw_client.get_recording_changes(listing.document_id, 18)
    assert changes.recordings[0].change_type == "update"


async def test_pvr_flag_actions(gw_client: SkyQBox) -> None:
    pvrid = "P2902aeb9"
    await gw_client.set_keep(pvrid)
    await gw_client.set_lock(pvrid)
    recording = await gw_client.get_recording(pvrid)
    assert recording.keep and recording.locked
    await gw_client.set_keep(pvrid, False)
    await gw_client.set_lock(pvrid, False)
    recording = await gw_client.get_recording(pvrid)
    assert not recording.keep and not recording.locked

    await gw_client.delete_recording(pvrid)
    assert (await gw_client.get_recording(pvrid)).is_deleted
    await gw_client.undelete_recording(pvrid)
    assert not (await gw_client.get_recording(pvrid)).is_deleted

    await gw_client.stop_recording(pvrid)
    await gw_client.erase_recording("P2901e45e")
    with pytest.raises(SkyQNotFoundError):
        await gw_client.get_recording("P2901e45e")


async def test_resume_position_is_forward_only(gw_client: SkyQBox, gateway: FakeSkyQ) -> None:
    await gw_client.set_resume_position("P2902aeb9", 2300)
    assert (await gw_client.get_recording("P2902aeb9")).resume_position == 2300
    with pytest.raises(ValueError, match="forward"):
        await gw_client.set_resume_position("P2902aeb9", 10)
    assert not any("pos" in q and q["pos"] == "10" for _, _, q in gateway.requests)  # never sent


async def test_booking_series_link_and_cancel(gw_client: SkyQBox) -> None:
    booked = await gw_client.book_recording("E81b-15a0")
    assert booked is not None and booked.status is RecordingStatus.SCHEDULED and booked.eid == "E81b-15a0"
    await gw_client.set_series_link(booked.pvrid)
    assert (await gw_client.get_recording(booked.pvrid)).series_linked
    await gw_client.set_series_link(booked.pvrid, False)
    await gw_client.cancel_booking(booked.pvrid)
    assert all(r.pvrid != booked.pvrid for r in (await gw_client.get_recordings()).recordings)

    series = await gw_client.book_recording("E81b-15a1", series=True)
    assert series is not None and series.series_linked
    ppv = await gw_client.book_ppv_recording("E1-1", "offer", pin="0000")
    assert ppv is not None


async def test_booking_not_found(gw_client: SkyQBox) -> None:
    with patch("aioskyq.client.asyncio.sleep"), patch.object(gw_client, "get_recordings") as listing:
        listing.return_value.recordings = ()
        await gw_client.rest.post("pvr/action/bookrecording", {"eid": "x"})
        assert await gw_client._find_booking("x") is None


async def test_erase_all_needs_confirm(gw_client: SkyQBox, gateway: FakeSkyQ) -> None:
    with pytest.raises(ValueError):
        await gw_client.erase_all_recordings()
    assert gateway.pvr["pvrItems"]
    await gw_client.erase_all_recordings(confirm=True)
    assert not gateway.pvr["pvrItems"]


async def test_now_playing_live_and_recording(gw_client: SkyQBox, gateway: FakeSkyQ) -> None:
    events = (await gw_client.get_schedule([3008], FIXTURE_DAY))[3008]
    with patch("aioskyq.epg.datetime") as fake_dt:
        fake_dt.now.return_value = events[4].start + timedelta(minutes=2)
        now = await gw_client.get_now_playing()
    assert now.media.sid == 3008 and now.channel and now.channel.number == "101"
    assert now.programme == events[4]
    assert now.transport.is_playing

    gateway.uri = "file://pvr/2902aeb9"
    now = await gw_client.get_now_playing()
    assert now.recording and now.recording.pvrid == "P2902aeb9" and now.position is not None

    gateway.uri = "xsi://FFFF"  # a channel the EPG does not know
    assert (await gw_client.get_now_playing(include_programme=False)).channel is None


async def test_controls(gw_client: SkyQBox, gateway: FakeSkyQ) -> None:
    await gw_client.change_channel(101)
    await wait_for(lambda: len(gateway.keys) == 4)
    assert gateway.keys == [Key.BACKUP, Key.DIGIT_1, Key.DIGIT_0, Key.DIGIT_1]
    await gw_client.tune(2075)  # BBC Two HD = 102
    await wait_for(lambda: len(gateway.keys) == 8)
    assert gateway.keys[-3:] == [Key.DIGIT_1, Key.DIGIT_0, Key.DIGIT_2]
    with pytest.raises(ValueError):
        await gw_client.change_channel("1O1")
    with pytest.raises(ValueError):
        await gw_client.tune(1)
    gateway.keys.clear()
    for action in (
        gw_client.play,
        gw_client.pause,
        gw_client.stop,
        gw_client.fast_forward,
        gw_client.rewind,
        gw_client.record,
    ):
        await action()
    await wait_for(lambda: len(gateway.keys) == 6)
    assert gateway.keys == [Key.PLAY, Key.PAUSE, Key.STOP, Key.FAST_FORWARD, Key.REWIND, Key.RECORD]


async def test_launch_app_and_push(gw_client: SkyQBox, gateway: FakeSkyQ) -> None:
    apps: list[AppsStatus] = []
    unsubscribe = gw_client.subscribe_apps(apps.append)
    await wait_for(lambda: len(apps) == 1)
    await gw_client.launch_app("YouTube")
    await wait_for(lambda: apps[-1].visible_app == "YouTube")
    assert ("POST", "apps/action/launch", {"appId": "YouTube"}) in gateway.requests
    unsubscribe()
    await asyncio.sleep(0.05)
    assert "apps/status" not in gw_client.streams_connected


async def test_push_subscriptions(gw_client: SkyQBox, gateway: FakeSkyQ) -> None:
    status: list[SystemStatus] = []
    pvr: list[PvrStatus] = []
    lists: list[dict[str, Any]] = []
    connections: list[tuple[str, bool]] = []

    async def on_pvr(event: PvrStatus) -> None:
        pvr.append(event)

    gw_client.subscribe_connection(lambda path, up: connections.append((path, up)))
    gw_client.subscribe_system_status(status.append)
    gw_client.subscribe_pvr(on_pvr)
    gw_client.subscribe_channel_list(lists.append)
    await wait_for(lambda: status and pvr and lists)
    assert status[0].is_active and pvr[0].document_id == "152"
    assert ("system/status", True) in connections
    assert all(gw_client.streams_connected.values())

    await gw_client.get_channels()
    await gateway.push("services/status", {"documentId": "2"})
    await wait_for(lambda: len(lists) == 2)
    assert gw_client._channels is None  # channel cache invalidated

    version = pvr[-1].max_version
    await gw_client.set_keep("P2902aeb9")
    await wait_for(lambda: pvr[-1].max_version == version + 1)


async def test_owned_session_and_context_manager(gateway: FakeSkyQ) -> None:
    async with SkyQBox("127.0.0.1", rest_port=gateway.rest_port) as box:
        assert (await box.get_device_info()).is_gateway
        assert box.territory == box.epg.territory == "GB"
        shared = EpgClient(box._session, "GB")
        box.epg = shared
        assert box.epg is shared
    assert box._session.closed


def test_fixture_day() -> None:
    assert date(2026, 10, 25) == FIXTURE_DAY
