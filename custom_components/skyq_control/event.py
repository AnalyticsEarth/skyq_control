"""Event entities: what changed on screen, and recordings starting, finishing or failing."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from homeassistant.components.event import EventEntity, EventEntityDescription
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import SkyQConfigEntry
from .coordinator import SkyQCoordinator
from .entity import SkyQEntity, async_remove_stale_entities
from .events import PLAYBACK_EVENTS, RECORDING_EVENTS

PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class SkyQEventDescription(EventEntityDescription):
    pvr: bool = False


EVENTS = (
    SkyQEventDescription(key="playback", translation_key="playback", event_types=list(PLAYBACK_EVENTS)),
    SkyQEventDescription(key="recordings", translation_key="recordings", event_types=list(RECORDING_EVENTS), pvr=True),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: SkyQConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    coordinator = entry.runtime_data
    wanted = [d for d in EVENTS if not d.pvr or coordinator.fetches_pvr]
    async_remove_stale_entities(hass, coordinator, Platform.EVENT, (d.key for d in EVENTS if d not in wanted))
    async_add_entities(SkyQEvent(coordinator, description) for description in wanted)


class SkyQEvent(SkyQEntity, EventEntity):
    entity_description: SkyQEventDescription

    def __init__(self, coordinator: SkyQCoordinator, description: SkyQEventDescription) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description
        self._attr_translation_key = description.translation_key

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if self.coordinator.events is not None:
            self.async_on_remove(self.coordinator.events.async_add_listener(self._on_event))

    @callback
    def _on_event(self, event_type: str, data: dict[str, Any]) -> None:
        if event_type in self.event_types:
            self._trigger_event(event_type, data)
            self.async_write_ha_state()
