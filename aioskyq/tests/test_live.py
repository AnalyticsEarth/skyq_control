"""Read-only checks against real boxes: SKYQ_HOST=10.0.0.5[,10.0.0.6] uv run pytest -m live

Nothing here presses keys or changes recordings.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from typing import Any

import pytest

from aioskyq import PowerState, SkyQBox, discover

HOSTS = [h.strip() for h in os.environ.get("SKYQ_HOST", "").split(",") if h.strip()]

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(not HOSTS, reason="set SKYQ_HOST to run live tests"),
]


@pytest.fixture(params=HOSTS or ["none"])
async def box(request: pytest.FixtureRequest) -> AsyncIterator[SkyQBox]:
    async with SkyQBox(request.param) as client:
        yield client


async def test_discovery_finds_hosts() -> None:
    found = {b.host for b in await discover()}
    assert set(HOSTS) <= found


async def test_reads(box: SkyQBox) -> None:
    info = await box.get_device_info()
    assert info.device_type in ("GATEWAYSTB", "MULTIROOMSTB") and info.as_version
    assert await box.ping()
    assert await box.get_power_state() in (PowerState.ON, PowerState.STANDBY)
    assert (await box.get_system_time()).utc is not None
    channels = await box.get_channels()
    assert len(channels) > 100 and await box.get_channel_by_number(101)
    await box.get_favourites()
    assert await box.get_genres() and await box.get_apps()
    recordings = await box.get_recordings()
    assert recordings.total == len(recordings.recordings)
    if recordings.recordings:
        first = recordings.recordings[0]
        assert (await box.get_recording(first.pvrid)).pvrid == first.pvrid
        await box.get_recording_changes(recordings.document_id or "", (recordings.version or 1) - 1)
    assert (await box.get_storage()).quota_mb > 0
    assert (await box.get_pin_status()).tries_left is not None
    await box.get_channel_details(channels[0].sid)


async def test_now_playing_with_guide(box: SkyQBox) -> None:
    now = await box.get_now_playing()
    assert now.transport.state_raw
    if now.media.sid is not None:
        assert now.channel is not None
        if now.channel.has_schedule and not now.channel.adult:
            assert now.programme is not None, f"no EPG for {now.channel.name}"


async def test_push_streams_snapshot(box: SkyQBox) -> None:
    got: dict[str, Any] = {}
    box.subscribe_system_status(lambda s: got.setdefault("system", s))
    box.subscribe_apps(lambda a: got.setdefault("apps", a))
    box.subscribe_pvr(lambda p: got.setdefault("pvr", p))
    box.subscribe_channel_list(lambda c: got.setdefault("services", c))
    async with asyncio.timeout(15):
        while len(got) < 4:
            await asyncio.sleep(0.1)
    assert got["system"].entitlements and got["pvr"].document_id
