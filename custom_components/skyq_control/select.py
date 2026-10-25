"""Channel and app pickers."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import SkyQConfigEntry
from .const import DOMAIN
from .coordinator import SkyQCoordinator
from .entity import SkyQEntity, channel_label, source_channels, user_apps, visible_app

PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant, entry: SkyQConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    coordinator = entry.runtime_data
    async_add_entities([SkyQChannelSelect(coordinator), SkyQAppSelect(coordinator)])


class SkyQChannelSelect(SkyQEntity, SelectEntity):
    """Favourite channels (or 101-199 when the box has no favourites)."""

    def __init__(self, coordinator: SkyQCoordinator) -> None:
        super().__init__(coordinator, "channel")

    @property
    def options(self) -> list[str]:
        return [channel_label(c) for c in source_channels(self.data)]

    @property
    def current_option(self) -> str | None:
        if self.data.channel and (label := channel_label(self.data.channel)) in self.options:
            return label
        return None

    async def async_select_option(self, option: str) -> None:
        channel = next((c for c in source_channels(self.data) if channel_label(c) == option), None)
        if channel is None:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="unknown_source", translation_placeholders={"source": option}
            )
        await self._command(self.coordinator.box.change_channel(channel.number))


class SkyQAppSelect(SkyQEntity, SelectEntity):
    """Installed apps; selecting one opens it."""

    def __init__(self, coordinator: SkyQCoordinator) -> None:
        super().__init__(coordinator, "app")

    @property
    def options(self) -> list[str]:
        return [app.title for app in user_apps(self.data)]

    @property
    def current_option(self) -> str | None:
        app = visible_app(self.data)
        return app.title if app and app.title in self.options else None

    async def async_select_option(self, option: str) -> None:
        app = next((a for a in user_apps(self.data) if a.title == option), None)
        if app is None:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="unknown_app", translation_placeholders={"app": option}
            )
        await self._command(self.coordinator.box.launch_app(app.app_id))
