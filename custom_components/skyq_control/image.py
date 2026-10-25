"""Images: the current programme's artwork and the current channel's logo (from Sky's public image hosts)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.image import ImageEntity, ImageEntityDescription
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from . import SkyQConfigEntry
from .coordinator import SkyQCoordinator, SkyQData
from .entity import SkyQEntity

PARALLEL_UPDATES = 0


def _artwork(data: SkyQData) -> str | None:
    if data.programme and data.programme.image_url:
        return data.programme.image_url
    return data.recording.image_url if data.recording else None


@dataclass(frozen=True, kw_only=True)
class SkyQImageDescription(ImageEntityDescription):
    url_fn: Callable[[SkyQData], str | None]


IMAGES: tuple[SkyQImageDescription, ...] = (
    SkyQImageDescription(key="programme_artwork", translation_key="programme_artwork", url_fn=_artwork),
    SkyQImageDescription(
        key="channel_logo",
        translation_key="channel_logo",
        url_fn=lambda data: data.channel.logo_url if data.channel else None,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: SkyQConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    async_add_entities(SkyQImage(entry.runtime_data, description) for description in IMAGES)


class SkyQImage(SkyQEntity, ImageEntity):
    """Unavailable while there's nothing to show (standby, an app, or no artwork)."""

    entity_description: SkyQImageDescription

    def __init__(self, coordinator: SkyQCoordinator, description: SkyQImageDescription) -> None:
        super().__init__(coordinator, description.key)
        ImageEntity.__init__(self, coordinator.hass)
        self.entity_description = description
        self._attr_translation_key = description.translation_key
        self._set_url(description.url_fn(coordinator.data))

    def _set_url(self, url: str | None) -> None:
        self._attr_image_url = url
        self._attr_image_last_updated = dt_util.utcnow() if url else None
        self._cached_image = None

    @property
    def available(self) -> bool:
        return super().available and self._attr_image_url is not None

    @callback
    def _handle_coordinator_update(self) -> None:
        url = self.entity_description.url_fn(self.data)
        if url != self._attr_image_url:
            self._set_url(url)
        super()._handle_coordinator_update()
