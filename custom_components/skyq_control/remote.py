"""Remote entity: every Sky Q remote key, usable from HA's remote card and remote.send_command."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from typing import Any

from homeassistant.components.remote import ATTR_DELAY_SECS, ATTR_NUM_REPEATS, RemoteEntity
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import SkyQConfigEntry
from .aioskyq import Key, PowerState
from .aioskyq.remote import resolve_key
from .const import DOMAIN
from .coordinator import SkyQCoordinator
from .entity import SkyQEntity

PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant, entry: SkyQConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    async_add_entities([SkyQRemote(entry.runtime_data)])


class SkyQRemote(SkyQEntity, RemoteEntity):
    _attr_name = None  # the device's name

    def __init__(self, coordinator: SkyQCoordinator) -> None:
        super().__init__(coordinator, "remote")

    @property
    def is_on(self) -> bool:
        return self.data.power is PowerState.ON

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"commands": [key.name.lower() for key in Key]}

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._command(self.coordinator.box.turn_on())

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._command(self.coordinator.box.turn_off())

    async def async_send_command(self, command: Iterable[str], **kwargs: Any) -> None:
        """Keys by name ("channel_up", "channelup", "7") or code; num_repeats and delay_secs honoured."""
        try:
            keys = [resolve_key(c) for c in command]
        except ValueError as err:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="unknown_key", translation_placeholders={"error": str(err)}
            ) from err
        repeats = int(kwargs.get(ATTR_NUM_REPEATS, 1))
        delay = float(kwargs.get(ATTR_DELAY_SECS, 0))
        box = self.coordinator.box

        async def send() -> None:
            for repeat in range(repeats):
                if repeat and delay:
                    await asyncio.sleep(delay)
                await box.press(keys)

        await self._command(send())
