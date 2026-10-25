"""Binary sensors: power, recording, playback source, the box's subsystems and its capabilities.

The box doesn't report HDMI or HDCP details (no `hdmi` section in system/status on current
firmware), so there are no sensors for them.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.const import EntityCategory, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import SkyQConfigEntry
from .aioskyq import RecordingStatus, SystemStatus
from .coordinator import SkyQCoordinator, SkyQData
from .entity import SkyQEntity, async_remove_stale_entities, recordings

PARALLEL_UPDATES = 0

AVAILABLE = "available"


@dataclass(frozen=True, kw_only=True)
class SkyQBinarySensorDescription(BinarySensorEntityDescription):
    value_fn: Callable[[SkyQData], bool | None]
    attributes_fn: Callable[[SkyQData], dict[str, Any] | None] = lambda _: None
    exists_fn: Callable[[SkyQCoordinator], bool] = lambda _: True
    pvr: bool = False
    status: bool = False


def _section_ok(name: str) -> Callable[[SkyQData], bool | None]:
    """A system/status section such as network or pvr reports state "available" when it's working."""

    def value(data: SkyQData) -> bool | None:
        state = data.system_status.section_state(name) if data.system_status else None
        return None if state is None else state == AVAILABLE

    return value


def _problem(name: str) -> Callable[[SkyQData], bool | None]:
    ok = _section_ok(name)

    def value(data: SkyQData) -> bool | None:
        result = ok(data)
        return None if result is None else not result

    return value


def _gateway_connected(status: SystemStatus) -> bool:
    services = status.gateway_services
    return services.get("state") == AVAILABLE and bool(services.get("paired"))


BINARY_SENSORS: tuple[SkyQBinarySensorDescription, ...] = (
    SkyQBinarySensorDescription(
        key="power",
        translation_key="power",
        device_class=BinarySensorDeviceClass.POWER,
        value_fn=lambda data: data.is_on,
    ),
    SkyQBinarySensorDescription(
        key="recording",
        translation_key="recording",
        value_fn=lambda data: bool(recordings(data, RecordingStatus.RECORDING)),
        pvr=True,
    ),
    SkyQBinarySensorDescription(
        key="watching_recording",
        translation_key="watching_recording",
        value_fn=lambda data: bool(data.media and data.media.is_recording),
    ),
    SkyQBinarySensorDescription(
        key="gateway_connection",
        translation_key="gateway_connection",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: _gateway_connected(data.system_status) if data.system_status else None,
        # e.g. "connection suspended for low power mode" while the Mini is in standby
        attributes_fn=lambda data: (
            {"reason": data.system_status.gateway_services.get("reason")} if data.system_status else None
        ),
        exists_fn=lambda coordinator: not coordinator.data.device.is_gateway,
        status=True,
    ),
    SkyQBinarySensorDescription(
        key="network",
        translation_key="network",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_section_ok("network"),
        status=True,
    ),
    SkyQBinarySensorDescription(
        key="recorder_problem",
        translation_key="recorder_problem",
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_problem("pvr"),
        status=True,
    ),
    SkyQBinarySensorDescription(
        key="drm_problem",
        translation_key="drm_problem",
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_problem("drmstatus"),
        status=True,
    ),
    SkyQBinarySensorDescription(
        key="guide_problem",
        translation_key="guide_problem",
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_problem("schedule"),
        status=True,
    ),
    SkyQBinarySensorDescription(
        key="software_update",
        translation_key="software_update",
        device_class=BinarySensorDeviceClass.UPDATE,
        entity_category=EntityCategory.DIAGNOSTIC,
        # "unavailable" (reason IDLE) when there's nothing to install.
        value_fn=lambda data: data.system_status.software_update_state == AVAILABLE if data.system_status else None,
        status=True,
    ),
    SkyQBinarySensorDescription(
        key="smartcard_active",
        translation_key="smartcard_active",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: (
            bool(data.system_status.smartcard.get("active") and data.system_status.smartcard.get("paired"))
            if data.system_status
            else None
        ),
        status=True,
    ),
    SkyQBinarySensorDescription(
        key="uhd_capable",
        translation_key="uhd_capable",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: data.device.uhd_capable,
    ),
    SkyQBinarySensorDescription(
        key="hdr_capable",
        translation_key="hdr_capable",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: data.device.hdr_capable,
    ),
    SkyQBinarySensorDescription(
        key="pip_capable",
        translation_key="pip_capable",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: data.device.pip_capable,
    ),
    SkyQBinarySensorDescription(
        key="streaming_capable",
        translation_key="streaming_capable",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: data.device.streaming_capable,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: SkyQConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    coordinator = entry.runtime_data
    wanted = [d for d in BINARY_SENSORS if d.exists_fn(coordinator) and (not d.pvr or coordinator.fetches_pvr)]
    async_remove_stale_entities(
        hass, coordinator, Platform.BINARY_SENSOR, (d.key for d in BINARY_SENSORS if d not in wanted)
    )
    async_add_entities(SkyQBinarySensor(coordinator, description) for description in wanted)


class SkyQBinarySensor(SkyQEntity, BinarySensorEntity):
    entity_description: SkyQBinarySensorDescription

    def __init__(self, coordinator: SkyQCoordinator, description: SkyQBinarySensorDescription) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description
        self._attr_translation_key = description.translation_key

    @property
    def available(self) -> bool:
        description = self.entity_description
        return super().available and self._has_data(pvr=description.pvr, status=description.status)

    @property
    def is_on(self) -> bool | None:
        return self.entity_description.value_fn(self.data)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        return self.entity_description.attributes_fn(self.data)
