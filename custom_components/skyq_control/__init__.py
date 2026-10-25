"""The Sky Q Control integration."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import CONF_HOST, Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.typing import ConfigType
from homeassistant.util.hass_dict import HassKey

from .aioskyq import DeviceInfo, EpgClient, SkyQBox
from .const import CONF_KEY_GAP, DEFAULT_KEY_GAP, DOMAIN
from .coordinator import SkyQCoordinator
from .entity import device_identifier, device_info_for
from .services import async_setup_services

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.CALENDAR,
    Platform.IMAGE,
    Platform.MEDIA_PLAYER,
    Platform.REMOTE,
    Platform.SELECT,
    Platform.SENSOR,
]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

# One guide client per territory, so every box in the house shares its cache (and Sky sees fewer requests).
GUIDE_CLIENTS: HassKey[dict[str, EpgClient]] = HassKey(f"{DOMAIN}_guide")


type SkyQConfigEntry = ConfigEntry[SkyQCoordinator]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the integration's actions (they target Sky Q media players)."""
    async_setup_services(hass)
    return True


def guide_client(hass: HomeAssistant, territory: str) -> EpgClient:
    clients = hass.data.setdefault(GUIDE_CLIENTS, {})
    if territory not in clients:
        clients[territory] = EpgClient(async_get_clientsession(hass), territory)
    return clients[territory]


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

    box.epg = guide_client(hass, box.territory)  # the territory is known once device info has been read
    entry.runtime_data = coordinator
    async_register_device(hass, entry, device)
    await coordinator.async_start_push()
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_migrate_entry(hass: HomeAssistant, entry: SkyQConfigEntry) -> bool:
    if entry.version > 1:
        return False  # a downgrade from a future version
    if entry.minor_version < 2:
        # 1.1 created most key buttons disabled; 1.2 enables them all. Buttons the user disabled stay disabled.
        registry = er.async_get(hass)
        for entity in er.async_entries_for_config_entry(registry, entry.entry_id):
            if entity.domain == Platform.BUTTON and entity.disabled_by is er.RegistryEntryDisabler.INTEGRATION:
                registry.async_update_entity(entity.entity_id, disabled_by=None)
        hass.config_entries.async_update_entry(entry, minor_version=2)
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
        other.runtime_data.async_update_listeners()  # its recorder entities now defer to the gateway


def _gateway_device_id(hass: HomeAssistant, gateway_ip: str) -> str | None:
    """Registry id of the gateway's device, if that gateway is configured and registered."""
    registry = dr.async_get(hass)
    for entry in hass.config_entries.async_entries(DOMAIN):
        if entry.data.get(CONF_HOST) == gateway_ip and entry.unique_id:
            device = registry.async_get_device_by_identifier((DOMAIN, entry.unique_id), entry.entry_id)
            if device is not None:
                return device.id
    return None
