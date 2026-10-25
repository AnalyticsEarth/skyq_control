"""Calendars: scheduled recordings (including any recording now) and recordings already made.

They live on the box that holds the household's recordings: the gateway, or a Mini whose gateway isn't set up.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from homeassistant.components.calendar import CalendarEntity, CalendarEntityDescription, CalendarEvent
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from . import SkyQConfigEntry
from .aioskyq import Recording, RecordingStatus
from .coordinator import SkyQCoordinator
from .entity import SkyQEntity, async_remove_stale_entities, recordings

PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class SkyQCalendarDescription(CalendarEntityDescription):
    statuses: tuple[RecordingStatus, ...]
    upcoming: bool  # the entity's `event` is the next one (bookings) rather than the latest (recordings made)


CALENDARS = (
    SkyQCalendarDescription(
        key="scheduled_recordings",
        translation_key="scheduled_recordings",
        statuses=(RecordingStatus.SCHEDULED, RecordingStatus.RECORDING),
        upcoming=True,
    ),
    SkyQCalendarDescription(
        key="recordings",
        translation_key="recordings",
        statuses=(RecordingStatus.RECORDED, RecordingStatus.PART_RECORDED),
        upcoming=False,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: SkyQConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    coordinator = entry.runtime_data
    if not coordinator.fetches_pvr:
        async_remove_stale_entities(hass, coordinator, Platform.CALENDAR, (d.key for d in CALENDARS))
        return
    async_add_entities(SkyQRecordingsCalendar(coordinator, description) for description in CALENDARS)


def to_event(recording: Recording) -> CalendarEvent | None:
    if recording.start is None or recording.end is None:
        return None
    details = [recording.synopsis or ""]
    if recording.season or recording.episode:
        details.append(f"Series {recording.season or '?'}, episode {recording.episode or '?'}")
    if recording.series_linked:
        details.append("Series linked")
    if recording.failure_reason and recording.failure_reason != "N/A":
        details.append(f"Note: {recording.failure_reason}")
    return CalendarEvent(
        start=recording.start,
        end=recording.end,
        summary=recording.title or "Recording",
        description="\n".join(line for line in details if line) or None,
        location=recording.channel_name,
        uid=recording.pvrid,
    )


class SkyQRecordingsCalendar(SkyQEntity, CalendarEntity):
    entity_description: SkyQCalendarDescription

    def __init__(self, coordinator: SkyQCoordinator, description: SkyQCalendarDescription) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description
        self._attr_translation_key = description.translation_key

    @property
    def available(self) -> bool:
        return super().available and self._has_data(pvr=True)

    def _events(self) -> list[CalendarEvent]:
        events = [to_event(r) for r in recordings(self.data, *self.entity_description.statuses)]
        return sorted((e for e in events if e is not None), key=lambda e: e.start_datetime_local)

    @property
    def event(self) -> CalendarEvent | None:
        events = self._events()
        if self.entity_description.upcoming:
            now = dt_util.now()
            return next((e for e in events if e.end_datetime_local > now), None)
        return events[-1] if events else None

    async def async_get_events(
        self, hass: HomeAssistant, start_date: datetime, end_date: datetime
    ) -> list[CalendarEvent]:
        return [e for e in self._events() if e.end_datetime_local > start_date and e.start_datetime_local < end_date]
