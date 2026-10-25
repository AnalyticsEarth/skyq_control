"""Programme guide from Sky's public metadata hosts.

The box itself has no guide endpoint (/as/schedule returns errorCode 104), so:
- hawk (awk.epgsky.com) serves every channel except UHD, up to 20 SIDs per request,
  from yesterday to about 8 days ahead;
- atlantis (atlantis.epgsky.com) serves UHD channels one SID at a time and needs the
  X-SkyOTT-Proposition / Provider / Territory headers.
URL dates are UTC days. Both hosts are unofficial, so results are cached.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Iterable
from datetime import UTC, date, datetime, timedelta
from typing import Any

import aiohttp

from .const import ATLANTIS_BASE, HAWK_BASE, HAWK_MAX_SIDS
from .exceptions import SkyQConnectionError, SkyQTimeoutError
from .models import Channel, EpgEvent

_LOGGER = logging.getLogger(__name__)

DEFAULT_TTL = 6 * 3600.0
PAST_DAY_TTL = 7 * 24 * 3600.0
EPG_TIMEOUT = 15.0

ChannelRef = int | str | Channel


class EpgClient:
    def __init__(
        self,
        session: aiohttp.ClientSession,
        territory: str = "GB",
        *,
        ttl: float = DEFAULT_TTL,
        hawk_base: str = HAWK_BASE,
        atlantis_base: str = ATLANTIS_BASE,
    ) -> None:
        self._session = session
        self.territory = territory
        self._ttl = ttl
        self._hawk_base = hawk_base.rstrip("/")
        self._atlantis_base = atlantis_base.rstrip("/")
        self._cache: dict[tuple[int, str], tuple[float, list[EpgEvent]]] = {}
        self._lock = asyncio.Lock()

    def clear_cache(self) -> None:
        self._cache.clear()

    async def get_day(self, channels: Iterable[ChannelRef], day: date) -> dict[int, list[EpgEvent]]:
        """Events for each channel on one UTC day. UHD `Channel`s are fetched from atlantis."""
        key = day.strftime("%Y%m%d")
        hawk_sids: list[int] = []
        atlantis_sids: list[int] = []
        result: dict[int, list[EpgEvent]] = {}
        now = time.monotonic()
        for ref in channels:
            sid = ref.sid if isinstance(ref, Channel) else int(ref)
            cached = self._cache.get((sid, key))
            if cached and cached[0] > now:
                result[sid] = cached[1]
            elif isinstance(ref, Channel) and ref.is_uhd:
                atlantis_sids.append(sid)
            else:
                hawk_sids.append(sid)

        async with self._lock:
            fetched: dict[int, list[EpgEvent]] = {}
            for start in range(0, len(hawk_sids), HAWK_MAX_SIDS):
                batch = hawk_sids[start : start + HAWK_MAX_SIDS]
                url = f"{self._hawk_base}/schedule/{key}/{','.join(map(str, batch))}"
                fetched.update(_parse_schedule(await self._get(url, {"X-SkyOTT-Territory": self.territory})))
            for sid in atlantis_sids:
                url = f"{self._atlantis_base}/schedule/{key}/{sid}"
                fetched.update(_parse_schedule(await self._get(url, self._atlantis_headers())))

        ttl = PAST_DAY_TTL if day < datetime.now(UTC).date() else self._ttl
        expires = time.monotonic() + ttl
        for sid in (*hawk_sids, *atlantis_sids):
            events = fetched.get(sid, [])
            self._cache[(sid, key)] = (expires, events)
            result[sid] = events
        return result

    async def get_events(self, channel: ChannelRef, start: datetime, end: datetime) -> list[EpgEvent]:
        """Events overlapping [start, end) on one channel, across UTC day boundaries."""
        sid = channel.sid if isinstance(channel, Channel) else int(channel)
        events: dict[str, EpgEvent] = {}
        day = start.astimezone(UTC).date()
        while day <= end.astimezone(UTC).date():
            for event in (await self.get_day([channel], day)).get(sid, []):
                if event.end > start and event.start < end:
                    events[event.eid] = event
            day += timedelta(days=1)
        return sorted(events.values(), key=lambda e: e.start)

    async def now_and_next(
        self, channel: ChannelRef, when: datetime | None = None
    ) -> tuple[EpgEvent | None, EpgEvent | None]:
        when = when or datetime.now(UTC)
        events = await self.get_events(channel, when - timedelta(hours=6), when + timedelta(hours=12))
        current = next((e for e in events if e.is_on_at(when)), None)
        upcoming = next((e for e in events if e.start >= (current.end if current else when)), None)
        return current, upcoming

    async def find_event(self, channel: ChannelRef, eid: str, days: int = 8) -> EpgEvent | None:
        today = datetime.now(UTC).date()
        sid = channel.sid if isinstance(channel, Channel) else int(channel)
        for offset in range(-1, days + 1):
            for event in (await self.get_day([channel], today + timedelta(days=offset))).get(sid, []):
                if event.eid == eid:
                    return event
        return None

    def _atlantis_headers(self) -> dict[str, str]:
        return {"X-SkyOTT-Proposition": "SKYQ", "X-SkyOTT-Provider": "SKY", "X-SkyOTT-Territory": self.territory}

    async def _get(self, url: str, headers: dict[str, str]) -> Any:
        _LOGGER.debug("EPG GET %s", url)
        try:
            async with self._session.get(
                url, headers=headers, timeout=aiohttp.ClientTimeout(total=EPG_TIMEOUT)
            ) as resp:
                if resp.status != 200:
                    raise SkyQConnectionError(f"EPG {url} returned HTTP {resp.status}")
                return await resp.json(content_type=None)
        except TimeoutError as err:
            raise SkyQTimeoutError(f"EPG {url} timed out") from err
        except aiohttp.ClientError as err:
            raise SkyQConnectionError(f"EPG {url} failed: {err}") from err


def _parse_schedule(data: Any) -> dict[int, list[EpgEvent]]:
    out: dict[int, list[EpgEvent]] = {}
    for schedule in (data or {}).get("schedule") or ():
        try:
            sid = int(schedule["sid"])
        except (KeyError, TypeError, ValueError):
            continue
        events = []
        for raw in schedule.get("events") or ():
            try:
                events.append(EpgEvent.from_api(sid, raw))
            except (KeyError, TypeError, ValueError):
                _LOGGER.debug("Skipping malformed EPG event on %s: %s", sid, raw)
        out[sid] = sorted(events, key=lambda e: e.start)
    return out
