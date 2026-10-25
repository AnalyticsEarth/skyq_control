"""Sensors: what's on, the recorder (storage and recordings) and the box's own status."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE, EntityCategory, Platform, UnitOfInformation
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.typing import StateType

from . import SkyQConfigEntry
from .aioskyq import EpgEvent, Recording, RecordingStatus, TransportState
from .coordinator import SkyQCoordinator, SkyQData
from .entity import SkyQEntity, async_remove_stale_entities, channel_label, recordings, visible_app

PARALLEL_UPDATES = 0  # read-only; the coordinator does the fetching

TRANSPORT_STATES = {
    TransportState.PLAYING: "playing",
    TransportState.PAUSED: "paused",
    TransportState.STOPPED: "stopped",
    TransportState.TRANSITIONING: "transitioning",
    TransportState.NO_MEDIA: "no_media",
}


@dataclass(frozen=True, kw_only=True)
class SkyQSensorDescription(SensorEntityDescription):
    value_fn: Callable[[SkyQData], StateType | date | datetime]
    attributes_fn: Callable[[SkyQData], dict[str, Any] | None] = lambda _: None
    exists_fn: Callable[[SkyQCoordinator], bool] = lambda _: True
    pvr: bool = False  # needs the household's recordings (fetched by the gateway only)
    status: bool = False  # needs the system/status push, which arrives just after setup


# ------------------------------------------------------------------ helpers


def next_recording(data: SkyQData) -> Recording | None:
    scheduled = [r for r in recordings(data, RecordingStatus.SCHEDULED) if r.start]
    return min(scheduled, key=lambda r: r.start or datetime.max, default=None)


def _event_attributes(event: EpgEvent) -> dict[str, Any]:
    return {
        "start": event.start.isoformat(),
        "end": event.end.isoformat(),
        "synopsis": event.synopsis,
        "season": event.season,
        "episode": event.episode,
        "event_id": event.eid,
        "image": event.image_url,
    }


def _programme_attributes(data: SkyQData) -> dict[str, Any] | None:
    if data.programme:
        return _event_attributes(data.programme)
    if recording := data.recording:
        return {
            "synopsis": recording.synopsis,
            "season": recording.season,
            "episode": recording.episode,
            "recording_id": recording.pvrid,
            "image": recording.image_url,
        }
    return None


def _channel(data: SkyQData) -> str | None:
    if data.channel:
        return channel_label(data.channel)
    return data.recording.channel_name if data.recording else None


def _transport(data: SkyQData) -> str | None:
    return TRANSPORT_STATES.get(data.transport.state) if data.transport else None


def _smartcard(data: SkyQData, key: str) -> Any:
    return data.system_status.smartcard.get(key) if data.system_status else None


def _software_update(data: SkyQData) -> str | None:
    status = data.system_status
    if status is None:
        return None
    value = status.software_update_reason or status.software_update_state
    return value.lower() if value else None


def _bouquet(data: SkyQData) -> str | None:
    device = data.device
    if device.bouquet is None:
        return None
    return f"{device.bouquet}/{device.subbouquet}" if device.subbouquet is not None else str(device.bouquet)


def _recording_titles(items: list[Recording]) -> dict[str, Any]:
    return {"titles": [r.title for r in items]}


def _next_recording_attributes(data: SkyQData) -> dict[str, Any] | None:
    if (recording := next_recording(data)) is None:
        return None
    return {
        "title": recording.title,
        "channel": recording.channel_name,
        "end": recording.end.isoformat() if recording.end else None,
        "recording_id": recording.pvrid,
        "series_linked": recording.series_linked,
    }


# ------------------------------------------------------------------ descriptions

_MB = {
    "device_class": SensorDeviceClass.DATA_SIZE,
    "native_unit_of_measurement": UnitOfInformation.MEGABYTES,
    "suggested_unit_of_measurement": UnitOfInformation.GIGABYTES,
    "suggested_display_precision": 0,
    "state_class": SensorStateClass.MEASUREMENT,
    "pvr": True,
}


def _count(key: str, *statuses: RecordingStatus) -> SkyQSensorDescription:
    return SkyQSensorDescription(
        key=key,
        translation_key=key,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: len(recordings(data, *statuses)),
        pvr=True,
    )


SENSORS: tuple[SkyQSensorDescription, ...] = (
    # What's on
    SkyQSensorDescription(
        key="channel",
        translation_key="current_channel",
        value_fn=_channel,
        attributes_fn=lambda data: (
            {"channel_number": data.channel.number, "service_id": data.channel.sid} if data.channel else None
        ),
    ),
    SkyQSensorDescription(
        key="programme",
        translation_key="programme",
        value_fn=lambda data: (
            data.programme.title if data.programme else data.recording.title if data.recording else None
        ),
        attributes_fn=_programme_attributes,
    ),
    SkyQSensorDescription(
        key="programme_end",
        translation_key="programme_end",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda data: data.programme.end if data.programme else None,
    ),
    SkyQSensorDescription(
        key="next_programme",
        translation_key="next_programme",
        value_fn=lambda data: data.next_programme.title if data.next_programme else None,
        attributes_fn=lambda data: _event_attributes(data.next_programme) if data.next_programme else None,
    ),
    SkyQSensorDescription(
        key="app",
        translation_key="app",
        value_fn=lambda data: app.title if (app := visible_app(data)) else None,
        attributes_fn=lambda data: {"app_id": app.app_id} if (app := visible_app(data)) else None,
    ),
    SkyQSensorDescription(
        key="transport_state",
        translation_key="transport_state",
        device_class=SensorDeviceClass.ENUM,
        options=list(TRANSPORT_STATES.values()),
        value_fn=_transport,
        attributes_fn=lambda data: (
            {"speed": data.transport.speed} if data.transport and data.transport.speed is not None else None
        ),
    ),
    # Recorder
    SkyQSensorDescription(
        key="storage_used_percent",
        translation_key="storage_used_percent",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=lambda data: data.storage.used_percent if data.storage else None,
        pvr=True,
    ),
    SkyQSensorDescription(
        key="storage_used",
        translation_key="storage_used",
        value_fn=lambda data: data.storage.used_mb if data.storage else None,
        **_MB,
    ),
    SkyQSensorDescription(
        key="storage_free",
        translation_key="storage_free",
        value_fn=lambda data: data.storage.free_mb if data.storage else None,
        **_MB,
    ),
    SkyQSensorDescription(
        key="storage_total",
        translation_key="storage_total",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: data.storage.quota_mb if data.storage else None,
        **_MB,
    ),
    _count("recordings_recorded", RecordingStatus.RECORDED, RecordingStatus.PART_RECORDED),
    _count("recordings_scheduled", RecordingStatus.SCHEDULED),
    _count("recordings_failed", RecordingStatus.FAILED),
    SkyQSensorDescription(
        key="recordings_deleted",
        translation_key="recordings_deleted",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: sum(1 for r in data.recordings.recordings if r.is_deleted) if data.recordings else None,
        pvr=True,
    ),
    SkyQSensorDescription(
        key="recording_now",
        translation_key="recording_now",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: len(recordings(data, RecordingStatus.RECORDING)),
        attributes_fn=lambda data: _recording_titles(recordings(data, RecordingStatus.RECORDING)),
        pvr=True,
    ),
    SkyQSensorDescription(
        key="next_recording",
        translation_key="next_recording",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda data: rec.start if (rec := next_recording(data)) else None,
        attributes_fn=_next_recording_attributes,
        pvr=True,
    ),
    SkyQSensorDescription(
        key="pin_tries_left",
        translation_key="pin_tries_left",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: data.pin.tries_left if data.pin else None,
        attributes_fn=lambda data: (
            {"seconds_to_unblock": data.pin.seconds_to_unblock} if data.pin and data.pin.seconds_to_unblock else None
        ),
        pvr=True,
    ),
    # The box
    SkyQSensorDescription(
        key="software_version",
        translation_key="software_version",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: data.device.as_version,
    ),
    SkyQSensorDescription(
        key="firmware_build",
        translation_key="firmware_build",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: data.device.version_number,
    ),
    SkyQSensorDescription(
        key="last_boot",
        translation_key="last_boot",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: data.boot_time,
    ),
    SkyQSensorDescription(
        key="wake_reason",
        translation_key="wake_reason",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: data.device.wake_reason,
    ),
    SkyQSensorDescription(
        key="gateway_address",
        translation_key="gateway_address",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: data.device.gateway_ip_address,
        exists_fn=lambda coordinator: not coordinator.data.device.is_gateway,
    ),
    SkyQSensorDescription(
        key="bouquet",
        translation_key="bouquet",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_bouquet,
        attributes_fn=lambda data: {"country": data.device.country_code},
    ),
    SkyQSensorDescription(
        key="guide_until",
        translation_key="guide_until",
        device_class=SensorDeviceClass.DATE,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: data.system_status.epg_last_date if data.system_status else None,
        status=True,
    ),
    SkyQSensorDescription(
        key="software_update",
        translation_key="software_update",
        entity_category=EntityCategory.DIAGNOSTIC,
        # The reason ("IDLE", ...) rather than the state, which is the literal word "unavailable" when idle.
        value_fn=_software_update,
        attributes_fn=lambda data: (
            {
                "state": data.system_status.software_update_state,
                "update_task": (data.system_status.raw.get("updatetask") or {}).get("state"),
                "update_task_reason": (data.system_status.raw.get("updatetask") or {}).get("reason"),
            }
            if data.system_status
            else None
        ),
        status=True,
    ),
    SkyQSensorDescription(
        key="entitlements",
        translation_key="entitlements",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: len(data.system_status.entitlements) if data.system_status else None,
        attributes_fn=lambda data: (
            {"entitlements": list(data.system_status.entitlements)} if data.system_status else None
        ),
        status=True,
    ),
    SkyQSensorDescription(
        key="smartcard",
        translation_key="smartcard",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: _smartcard(data, "state"),
        attributes_fn=lambda data: (
            {
                "active": _smartcard(data, "active"),
                "paired": _smartcard(data, "paired"),
                "currency": _smartcard(data, "currency"),
                "transaction_limit": _smartcard(data, "transactionlimit"),
            }
            if data.system_status
            else None
        ),
        status=True,
    ),
    SkyQSensorDescription(
        key="household_id",
        translation_key="household_id",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda data: _smartcard(data, "householdid"),
        status=True,
    ),
    SkyQSensorDescription(
        key="viewing_card",
        translation_key="viewing_card",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda data: _smartcard(data, "viewingCardNumber"),
        status=True,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: SkyQConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    coordinator = entry.runtime_data
    wanted = [d for d in SENSORS if d.exists_fn(coordinator) and (not d.pvr or coordinator.fetches_pvr)]
    async_remove_stale_entities(hass, coordinator, Platform.SENSOR, (d.key for d in SENSORS if d not in wanted))
    async_add_entities(SkyQSensor(coordinator, description) for description in wanted)


class SkyQSensor(SkyQEntity, SensorEntity):
    entity_description: SkyQSensorDescription

    def __init__(self, coordinator: SkyQCoordinator, description: SkyQSensorDescription) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description
        self._attr_translation_key = description.translation_key

    @property
    def available(self) -> bool:
        description = self.entity_description
        return super().available and self._has_data(pvr=description.pvr, status=description.status)

    @property
    def native_value(self) -> StateType | date | datetime:
        return self.entity_description.value_fn(self.data)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        return self.entity_description.attributes_fn(self.data)
