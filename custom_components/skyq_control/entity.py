"""Shared entity helpers for Sky Q Control."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Iterable
from typing import Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import CONNECTION_NETWORK_MAC, DeviceInfo, format_mac
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .aioskyq import App, Channel, Recording, RecordingStatus, SkyQError
from .aioskyq import DeviceInfo as SkyQDeviceInfo
from .const import DOMAIN
from .coordinator import SkyQCoordinator, SkyQData

# Sky's own TV interface: "visible" whenever you're watching live TV or browsing the guide.
TV_APP = "com.bskyb.epgui"
# Background and service apps that should never be offered as something to open.
SYSTEM_APP_PREFIXES = (
    "com.bskyb.broadcastmetrics",
    "com.bskyb.ca-bundle",
    "com.bskyb.dsu",
    "com.bskyb.localstore",
    "com.bskyb.metric",
    "com.juno.",
    "com.sky.factorytest",
    "audiostreamer",
)
# The box applies a key press a moment after receiving it; refresh after that.
REFRESH_AFTER_COMMAND = 2.0


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


def channel_label(channel: Channel) -> str:
    return f"{channel.number} {channel.name}"


def _channel_order(channel: Channel) -> tuple[bool, int, str]:
    number = int(channel.number) if channel.number.isdigit() else 10_000
    return (channel.is_radio, number, channel.name)


def source_channels(data: SkyQData) -> list[Channel]:
    """Every channel on the box: TV by number, then radio (whose numbers start with 0)."""
    channels = sorted((c for c in data.channels.values() if c.number.isdigit()), key=_channel_order)
    # Labels identify a source, so drop the (unlikely) second channel with the same number and name.
    unique: dict[str, Channel] = {}
    for channel in channels:
        unique.setdefault(channel_label(channel), channel)
    return list(unique.values())


def favourite_channels(data: SkyQData) -> list[Channel]:
    """The box's favourites in its own order (Minis have none)."""
    return [data.channels[f.sid] for f in sorted(data.favourites, key=lambda f: f.position) if f.sid in data.channels]


def recordings(data: SkyQData, *statuses: RecordingStatus) -> list[Recording]:
    """Recordings not in the trash, optionally of the given statuses."""
    items = data.recordings.recordings if data.recordings else ()
    return [r for r in items if not r.is_deleted and (not statuses or r.status in statuses)]


def household_recordings(coordinator: SkyQCoordinator) -> list[Recording] | None:
    """Every recording in the house (the gateway holds them), or None if they haven't been fetched."""
    pvr = coordinator.household_pvr
    if pvr is None or pvr.data.recordings is None:
        return None
    return list(pvr.data.recordings.recordings)


def user_apps(data: SkyQData) -> list[App]:
    apps = [
        app
        for app in data.apps.values()
        if app.app_id != TV_APP and not app.app_id.startswith(SYSTEM_APP_PREFIXES) and not app.blocked
    ]
    return sorted(apps, key=lambda app: app.title.casefold())


def visible_app(data: SkyQData) -> App | None:
    """The app on screen, or None while watching TV."""
    if not data.visible_app or data.visible_app == TV_APP:
        return None
    return data.apps.get(data.visible_app) or App.from_api({"appId": data.visible_app})


def unique_id_for(coordinator: SkyQCoordinator, key: str) -> str:
    return f"{coordinator.data.device.unique_id or coordinator.config_entry.entry_id}_{key}"


@callback
def async_remove_stale_entities(
    hass: HomeAssistant, coordinator: SkyQCoordinator, domain: str, keys: Iterable[str]
) -> None:
    """Drop entities this box no longer gets (a Mini's recorder entities once its gateway is set up)."""
    registry = er.async_get(hass)
    for key in keys:
        if entity_id := registry.async_get_entity_id(domain, DOMAIN, unique_id_for(coordinator, key)):
            registry.async_remove(entity_id)


class SkyQEntity(CoordinatorEntity[SkyQCoordinator]):
    """Base class: one device per box, names come from translation keys."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: SkyQCoordinator, key: str) -> None:
        super().__init__(coordinator)
        device = coordinator.data.device
        self._attr_unique_id = unique_id_for(coordinator, key)
        self._attr_translation_key = key
        self._attr_device_info = DeviceInfo(identifiers={device_identifier(device, coordinator.host)})

    @property
    def data(self) -> SkyQData:
        return self.coordinator.data

    def _has_data(self, *, pvr: bool = False, status: bool = False) -> bool:
        """pvr: needs the household's recordings, which only the gateway fetches once it's set up.
        status: needs the system/status push, which arrives just after setup."""
        if pvr and (not self.coordinator.fetches_pvr or self.data.recordings is None):
            return False
        return not (status and self.data.system_status is None)

    async def _command(self, action: Awaitable[Any]) -> None:
        """Run a box command, surface failures to the user, and refresh once the box has reacted."""
        try:
            await action
        except SkyQError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="command_failed",
                translation_placeholders={"error": str(err)},
            ) from err
        self.coordinator.config_entry.async_create_background_task(
            self.hass, self._refresh_soon(), f"{DOMAIN} refresh after command"
        )

    async def _refresh_soon(self) -> None:
        await asyncio.sleep(REFRESH_AFTER_COMMAND)
        await self.coordinator.async_request_refresh()
