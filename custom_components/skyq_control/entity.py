"""Shared entity helpers for Sky Q Control."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable

from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import CONNECTION_NETWORK_MAC, DeviceInfo, format_mac
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .aioskyq import App, Channel, SkyQError
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


def source_channels(data: SkyQData) -> list[Channel]:
    """Channels to offer as sources: the box's favourites, else the entertainment range (101-199)."""
    favourites = [data.channels[f.sid] for f in data.favourites if f.sid in data.channels]
    if favourites:
        return favourites
    tv = [c for c in data.channels.values() if c.number.isdigit() and 101 <= int(c.number) <= 199 and not c.is_radio]
    return sorted(tv, key=lambda c: int(c.number))


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


class SkyQEntity(CoordinatorEntity[SkyQCoordinator]):
    """Base class: one device per box, names come from translation keys."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: SkyQCoordinator, key: str) -> None:
        super().__init__(coordinator)
        device = coordinator.data.device
        self._attr_unique_id = f"{device.unique_id or coordinator.config_entry.entry_id}_{key}"
        self._attr_translation_key = key
        self._attr_device_info = DeviceInfo(identifiers={device_identifier(device, coordinator.host)})

    @property
    def data(self) -> SkyQData:
        return self.coordinator.data

    async def _command(self, action: Awaitable[None]) -> None:
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
