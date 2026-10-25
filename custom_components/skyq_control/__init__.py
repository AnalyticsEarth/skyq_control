"""The Sky Q Control integration."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import CONF_HOST, Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .aioskyq import DeviceInfo, SkyQBox
from .const import CONF_KEY_GAP, DEFAULT_KEY_GAP, DOMAIN
from .coordinator import SkyQCoordinator
from .entity import device_identifier, device_info_for

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = []  # media_player, remote, button, sensor… arrive in later phases

type SkyQConfigEntry = ConfigEntry[SkyQCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: SkyQConfigEntry) -> bool:
    box = SkyQBox(
        entry.data[CONF_HOST],
        async_get_clientsession(hass),
        key_gap=entry.options.get(CONF_KEY_GAP, DEFAULT_KEY_GAP),
    )
    coordinator = SkyQCoordinator(hass, entry, box)
    try:
        await coordinator.async_config_entry_first_refresh()
    except Exception:
        await box.close()
        raise

    device = coordinator.data.device
    if entry.unique_id and device.unique_id and device.unique_id != entry.unique_id:
        await box.close()
        raise ConfigEntryError(
            translation_domain=DOMAIN,
            translation_key="wrong_device",
            translation_placeholders={"host": entry.data[CONF_HOST]},
        )

    entry.runtime_data = coordinator
    async_register_device(hass, entry, device)
    await coordinator.async_start_push()
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: SkyQConfigEntry) -> bool:
    # The coordinator's async_shutdown (registered automatically) stops streams and closes the box.
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


@callback
def async_register_device(hass: HomeAssistant, entry: SkyQConfigEntry, device: DeviceInfo) -> None:
    """Create or update the box's device, and link Minis to their gateway."""
    registry = dr.async_get(hass)
    via_device_id = None
    if not device.is_gateway and device.gateway_ip_address:
        via_device_id = _gateway_device_id(hass, device.gateway_ip_address)
    registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        **device_info_for(device, entry.data[CONF_HOST]),
        **({"via_device_id": via_device_id} if via_device_id else {}),
    )

    if not device.is_gateway:
        return
    # Minis added before their gateway get linked now.
    gateway = registry.async_get_device_by_identifier(device_identifier(device, entry.data[CONF_HOST]), entry.entry_id)
    if gateway is None:
        return
    for other in hass.config_entries.async_entries(DOMAIN):
        if other.entry_id == entry.entry_id or other.state is not ConfigEntryState.LOADED:
            continue
        other_device = other.runtime_data.data.device
        if other_device.gateway_ip_address != entry.data[CONF_HOST]:
            continue
        mini = registry.async_get_device_by_identifier(
            device_identifier(other_device, other.data[CONF_HOST]), other.entry_id
        )
        if mini is not None and mini.via_device_id != gateway.id:
            registry.async_update_device(mini.id, via_device_id=gateway.id)


def _gateway_device_id(hass: HomeAssistant, gateway_ip: str) -> str | None:
    """Registry id of the gateway's device, if that gateway is configured and registered."""
    registry = dr.async_get(hass)
    for entry in hass.config_entries.async_entries(DOMAIN):
        if entry.data.get(CONF_HOST) == gateway_ip and entry.unique_id:
            device = registry.async_get_device_by_identifier((DOMAIN, entry.unique_id), entry.entry_id)
            if device is not None:
                return device.id
    return None
