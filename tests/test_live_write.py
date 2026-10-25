"""Recording actions end to end on a real gateway, through Home Assistant. WRITES to the box.

Books one short overnight news programme from the guide, keeps and locks it, then deletes it and makes
sure nothing is left behind. It never touches any other recording.

SKYQ_GATEWAY=192.168.1.50 SKYQ_WRITE=1 uv run pytest tests/test_live_write.py -m live -s
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable
from datetime import timedelta
from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.skyq_control.const import DOMAIN

from .test_live import GATEWAY, allow_boxes  # noqa: F401  (fixture)

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        not (GATEWAY and os.environ.get("SKYQ_WRITE") == "1"), reason="set SKYQ_GATEWAY and SKYQ_WRITE=1"
    ),
]

NEWS_CHANNELS = ["501", "502", "503", "504"]  # overnight news: low value, nobody minds a test booking
TIMEOUT = 45


async def test_live_book_keep_lock_delete(hass: HomeAssistant, allow_boxes: None) -> None:  # noqa: F811
    import aiohttp
    from homeassistant.setup import async_setup_component

    from custom_components.skyq_control.aioskyq import SkyQBox

    await hass.config.async_set_time_zone("Europe/London")  # the harness defaults to US time; "overnight" is UK
    session = aiohttp.ClientSession()
    patcher = patch("custom_components.skyq_control.async_get_clientsession", return_value=session)
    patcher.start()
    async with SkyQBox(GATEWAY) as probe:
        unique_id = (await probe.get_device_info()).unique_id
    entry = MockConfigEntry(domain=DOMAIN, unique_id=unique_id, data={CONF_HOST: GATEWAY}, title="gateway")
    entry.add_to_hass(hass)
    assert await async_setup_component(hass, DOMAIN, {})
    await hass.async_block_till_done()
    coordinator = entry.runtime_data
    player = next(
        e.entity_id
        for e in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
        if e.domain == "media_player"
    )

    async def action(name: str, **data: Any) -> Any:
        response = name.startswith("get_")
        print(f"  -> {name} {data or ''}")
        result = await hass.services.async_call(
            DOMAIN, name, {"entity_id": player, **data}, blocking=True, return_response=response
        )
        return result[player] if response else None

    async def find(event_id: str) -> list[dict[str, Any]]:
        """Every PVR item for this guide event, including deleted ones, from a fresh read of the box."""
        coordinator.mark_stale("pvr")
        await coordinator.async_refresh()
        result = await action(
            "get_recordings", status=["scheduled", "recording", "recorded", "failed", "deleted"], limit=2000
        )
        return [r for r in result["recordings"] if r["event_id"] == event_id]

    async def wait_for(event_id: str, check: Callable[[list[dict[str, Any]]], bool], what: str) -> list[dict]:
        async with asyncio.timeout(TIMEOUT):
            while not check(items := await find(event_id)):
                await asyncio.sleep(3)
        print(f"     ok: {what}")
        return items

    booked_id: str | None = None
    event_id: str | None = None
    try:
        # 1. Pick a short overnight news programme that isn't already booked.
        tomorrow = dt_util.start_of_local_day() + timedelta(days=1)
        guide = await action(
            "get_guide", channels=NEWS_CHANNELS, start=(tomorrow + timedelta(hours=2)).isoformat(), hours=3
        )
        existing = {
            r["event_id"] for r in (await action("get_recordings", status="scheduled", limit=2000))["recordings"]
        }
        candidates = [
            p
            for p in guide["programmes"]
            if p["can_record"]
            and p["event_id"] not in existing
            and dt_util.parse_datetime(p["end"]) - dt_util.parse_datetime(p["start"]) <= timedelta(hours=1)
        ]
        assert candidates, "no short overnight news programme to book"
        programme = candidates[0]
        event_id = programme["event_id"]
        print(
            f"\nTest booking: {programme['channel_number']} {programme['channel']} {programme['start']} "
            f"'{programme['title']}' ({event_id})"
        )
        assert not await find(event_id)

        # 2. Book it.
        await action("record", event_id=event_id)
        items = await wait_for(event_id, lambda i: any(r["status"] == "scheduled" for r in i), "booking appeared")
        booked = next(r for r in items if r["status"] == "scheduled")
        booked_id = booked["recording_id"]
        assert not booked["keep"] and not booked["locked"]
        calendar = hass.states.get("calendar.sky_q_gateway_scheduled_recordings")
        print(f"     scheduled calendar: {calendar.state}, next: {calendar.attributes.get('message')}")

        # 3. Keep and lock it.
        await action("update_recording", recording_id=booked_id, keep=True, locked=True)
        await wait_for(
            event_id,
            lambda i: any(r["recording_id"] == booked_id and r["keep"] and r["locked"] for r in i),
            "kept and locked",
        )

        # 4. Delete it (cancels the booking), then check nothing is left, in the trash or anywhere.
        await action("update_recording", recording_id=booked_id, keep=False, locked=False)
        await wait_for(
            event_id,
            lambda i: any(r["recording_id"] == booked_id and not r["keep"] and not r["locked"] for r in i),
            "unkept and unlocked",
        )
        await action("delete_recording", recording_id=booked_id)
        leftovers = await wait_for(event_id, lambda i: all(r["status"] == "deleted" for r in i), "booking cancelled")
        for item in leftovers:  # a cancelled booking may sit in the trash: erase it
            await action("delete_recording", recording_id=item["recording_id"], permanent=True)
        await wait_for(event_id, lambda i: not i, "nothing left behind")
        booked_id = None
    finally:
        if booked_id and event_id:  # something failed part-way: put the box back as it was
            print(f"  cleaning up {booked_id}")
            box = coordinator.box
            for step in (
                lambda: box.set_lock(booked_id, False),
                lambda: box.set_keep(booked_id, False),
                lambda: box.delete_recording(booked_id),
            ):
                try:
                    await step()
                except Exception as err:
                    print(f"  cleanup step failed: {err}")
            await asyncio.sleep(5)
            for item in await find(event_id):
                await box.erase_recording(item["recording_id"])
            print(f"  left after cleanup: {await find(event_id)}")
        await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
        patcher.stop()
        await session.close()
