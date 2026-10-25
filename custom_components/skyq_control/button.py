"""One button per Sky Q remote key."""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import SkyQConfigEntry
from .aioskyq import Key
from .coordinator import SkyQCoordinator
from .entity import SkyQEntity

PARALLEL_UPDATES = 1

# Keys most dashboards need; the rest are created disabled so four boxes don't flood the UI.
ENABLED_BY_DEFAULT = {
    Key.POWER,
    Key.SELECT,
    Key.BACKUP,
    Key.UP,
    Key.DOWN,
    Key.LEFT,
    Key.RIGHT,
    Key.CHANNEL_UP,
    Key.CHANNEL_DOWN,
    Key.PLAY,
    Key.PAUSE,
    Key.STOP,
    Key.RECORD,
    Key.FAST_FORWARD,
    Key.REWIND,
    Key.TV_GUIDE,
    Key.SKY,
    Key.I,
    Key.RED,
    Key.GREEN,
    Key.YELLOW,
    Key.BLUE,
    Key.RED_BUTTON,
}


@dataclass(frozen=True, kw_only=True)
class SkyQButtonDescription(ButtonEntityDescription):
    sky_key: Key


BUTTONS = tuple(
    SkyQButtonDescription(
        key=f"key_{key.name.lower()}",
        translation_key=f"key_{key.name.lower()}",
        sky_key=key,
        entity_registry_enabled_default=key in ENABLED_BY_DEFAULT,
    )
    for key in Key
)


async def async_setup_entry(
    hass: HomeAssistant, entry: SkyQConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(SkyQKeyButton(coordinator, description) for description in BUTTONS)


class SkyQKeyButton(SkyQEntity, ButtonEntity):
    entity_description: SkyQButtonDescription

    def __init__(self, coordinator: SkyQCoordinator, description: SkyQButtonDescription) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    async def async_press(self) -> None:
        await self._command(self.coordinator.box.press(self.entity_description.sky_key))
