"""Shared entity helpers for Sky Q Control."""

from __future__ import annotations

from homeassistant.helpers.device_registry import CONNECTION_NETWORK_MAC, DeviceInfo, format_mac
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from aioskyq import DeviceInfo as SkyQDeviceInfo

from .const import DOMAIN
from .coordinator import SkyQCoordinator


def device_identifier(device: SkyQDeviceInfo, host: str) -> tuple[str, str]:
    return (DOMAIN, device.unique_id or host)


def device_info_for(device: SkyQDeviceInfo, host: str) -> DeviceInfo:
    """Registry fields for a box. Minis get via_device_id (the gateway's device) at registration."""
    info = DeviceInfo(
        identifiers={device_identifier(device, host)},
        manufacturer=device.manufacturer or "Sky",
        model="Sky Q Gateway" if device.is_gateway else "Sky Q Mini",
        model_id=device.hardware_model,
        name="Sky Q Gateway" if device.is_gateway else f"Sky Q Mini ({host})",
        sw_version=device.as_version,
        hw_version=device.hardware_name,
        serial_number=device.serial_number,
    )
    if device.mac_address:
        info["connections"] = {(CONNECTION_NETWORK_MAC, format_mac(device.mac_address))}
    return info


class SkyQEntity(CoordinatorEntity[SkyQCoordinator]):
    """Base class: one device per box, names come from translation keys."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: SkyQCoordinator, key: str) -> None:
        super().__init__(coordinator)
        device = coordinator.data.device
        self._attr_unique_id = f"{device.unique_id or coordinator.config_entry.entry_id}_{key}"
        self._attr_translation_key = key
        self._attr_device_info = DeviceInfo(identifiers={device_identifier(device, coordinator.host)})
