"""Data coordinator for one Sky Q box: polled playback state plus websocket pushes."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .aioskyq import (
    App,
    AppsStatus,
    Channel,
    DeviceInfo,
    EpgEvent,
    Favourite,
    MediaInfo,
    PinStatus,
    PositionInfo,
    PowerState,
    PvrStatus,
    Recording,
    RecordingList,
    SkyQBox,
    SkyQConnectionError,
    SkyQError,
    Storage,
    SystemStatus,
    TransportInfo,
)
from .const import (
    CONF_SCAN_INTERVAL,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    REFRESH_CATALOGUE,
    REFRESH_DEVICE,
    REFRESH_PVR,
    STANDBY_SCAN_INTERVAL,
)

if TYPE_CHECKING:
    from . import SkyQConfigEntry

_LOGGER = logging.getLogger(__name__)

BOOT_TIME_TOLERANCE = timedelta(minutes=2)


@dataclass(slots=True)
class SkyQData:
    """Everything the entities read. Replaced (never mutated in place) on each update."""

    device: DeviceInfo
    power: PowerState
    boot_time: datetime | None = None  # worked out when device info is read, not on every access
    # Playback (polled; channel changes are not pushed by the box)
    transport: TransportInfo | None = None
    media: MediaInfo | None = None
    channel: Channel | None = None
    programme: EpgEvent | None = None
    next_programme: EpgEvent | None = None
    recording: Recording | None = None  # the recording being played, if any
    position: PositionInfo | None = None
    position_at: datetime | None = None  # when `position` was read
    # Pushed
    visible_app: str | None = None
    system_status: SystemStatus | None = None
    # Catalogue (slow)
    channels: dict[int, Channel] = field(default_factory=dict)
    favourites: list[Favourite] = field(default_factory=list)
    genres: dict[int, str] = field(default_factory=dict)  # channel genre (sg) -> name
    apps: dict[str, App] = field(default_factory=dict)
    # PVR (gateway, or a Mini whose gateway isn't configured)
    recordings: RecordingList | None = None
    storage: Storage | None = None
    pin: PinStatus | None = None

    @property
    def is_on(self) -> bool:
        return self.power is PowerState.ON


class SkyQCoordinator(DataUpdateCoordinator[SkyQData]):
    config_entry: SkyQConfigEntry

    def __init__(self, hass: HomeAssistant, entry: SkyQConfigEntry, box: SkyQBox) -> None:
        self.box = box
        self.host: str = entry.data[CONF_HOST]
        self._on_interval = timedelta(seconds=entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL))
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} {self.host}",
            update_interval=self._on_interval,
            always_update=False,
        )
        self._due: dict[str, float] = {}  # monotonic time each slow group is next due
        self._unsubscribe: list[Callable[[], None]] = []

    # ------------------------------------------------------------------ polling

    def _is_due(self, group: str) -> bool:
        return time.monotonic() >= self._due.get(group, 0)

    def _mark(self, group: str, seconds: float) -> None:
        self._due[group] = time.monotonic() + seconds

    def mark_stale(self, *groups: str) -> None:
        for group in groups:
            self._due[group] = 0

    @property
    def fetches_pvr(self) -> bool:
        return self.data is not None and self._fetches_pvr_for(self.data.device)

    def _fetches_pvr_for(self, device: DeviceInfo) -> bool:
        """Recordings are shared, so only the gateway fetches them (or a Mini whose gateway isn't set up)."""
        if device.is_gateway or not device.gateway_ip_address:
            return True
        return not any(
            entry.data.get(CONF_HOST) == device.gateway_ip_address
            for entry in self.hass.config_entries.async_entries(DOMAIN)
            if entry.entry_id != self.config_entry.entry_id and not entry.disabled_by
        )

    @property
    def household_pvr(self) -> SkyQCoordinator | None:
        """The coordinator holding this household's recordings: this box's, or its gateway's."""
        if self.fetches_pvr:
            return self
        gateway_ip = self.data.device.gateway_ip_address if self.data else None
        for entry in self.hass.config_entries.async_loaded_entries(DOMAIN):
            if entry.data.get(CONF_HOST) == gateway_ip and entry.runtime_data.fetches_pvr:
                return entry.runtime_data
        return None

    async def _async_update_data(self) -> SkyQData:
        try:
            power = await self.box.get_power_state()
            if power is PowerState.OFF:
                raise UpdateFailed(
                    translation_domain=DOMAIN,
                    translation_key="unreachable",
                    translation_placeholders={"host": self.host},
                )
            fetch_device = self.data is None or self._is_due("device")
            if fetch_device:
                device = await self.box.get_device_info(refresh=self.data is not None)
                self._mark("device", REFRESH_DEVICE)
            else:
                device = self.data.device
            data = replace(self.data, device=device, power=power) if self.data else SkyQData(device=device, power=power)
            if fetch_device:  # uptime is only fresh straight after a fetch
                data.boot_time = self._boot_time(device, data.boot_time)
            await self._refresh_catalogue(data)
            await self._refresh_pvr(data)
            if power is PowerState.ON:
                await self._refresh_playback(data)
            else:
                data.transport = data.media = data.channel = data.programme = data.next_programme = None
                data.recording = data.position = None
        except SkyQConnectionError as err:
            raise UpdateFailed(
                translation_domain=DOMAIN, translation_key="unreachable", translation_placeholders={"host": self.host}
            ) from err
        if self.data is not None:
            # Pushed fields may have changed while this poll was awaiting the box; keep the newest.
            data.system_status = self.data.system_status
            data.visible_app = self.data.visible_app
        self.update_interval = self._on_interval if power is PowerState.ON else STANDBY_SCAN_INTERVAL
        return data

    @staticmethod
    def _boot_time(device: DeviceInfo, previous: datetime | None) -> datetime | None:
        """Boot time from uptime, kept steady: the box reports uptime in seconds and polls drift slightly."""
        boot_time = device.boot_time
        if boot_time is None:
            return None
        if previous is not None and abs(boot_time - previous) < BOOT_TIME_TOLERANCE:
            return previous
        return boot_time.replace(microsecond=0)

    async def _refresh_catalogue(self, data: SkyQData) -> None:
        if not self._is_due("catalogue"):
            return
        channels, favourites, genres, apps = await asyncio.gather(
            self.box.get_channels(refresh=True), self.box.get_favourites(), self.box.get_genres(), self.box.get_apps()
        )
        data.channels = {channel.sid: channel for channel in channels}
        data.favourites = favourites
        data.genres = {genre.genre: genre.title for genre in genres}
        data.apps = {app.app_id: app for app in apps}
        self._mark("catalogue", REFRESH_CATALOGUE)

    async def _refresh_pvr(self, data: SkyQData) -> None:
        if not self._is_due("pvr"):
            return
        self._mark("pvr", REFRESH_PVR)  # also on failure, so a slow PVR doesn't block every poll
        if not self._fetches_pvr_for(data.device):
            return
        try:
            data.recordings, data.storage, data.pin = await asyncio.gather(
                self.box.get_recordings(), self.box.get_storage(), self.box.get_pin_status()
            )
        except SkyQConnectionError:
            raise
        except SkyQError as err:
            _LOGGER.debug("PVR refresh on %s failed: %s", self.host, err)

    async def _refresh_playback(self, data: SkyQData) -> None:
        previous = self.data
        try:
            data.transport, data.media = await asyncio.gather(self.box.get_transport_info(), self.box.get_media_info())
        except SkyQConnectionError:
            raise
        except SkyQError as err:  # SOAP faults: keep the rest of the update
            _LOGGER.debug("Playback state on %s unavailable: %s", self.host, err)
            data.transport = data.media = None
        media = data.media

        if media is not None and media.sid is not None:
            data.channel = data.channels.get(media.sid)
            data.recording = data.position = None
            now = datetime.now(UTC)
            same_channel = previous is not None and previous.media is not None and previous.media.sid == media.sid
            programme_current = data.programme is not None and data.programme.is_on_at(now)
            if not (same_channel and programme_current):
                await self._refresh_programme(data, media.sid, now)
        elif media is not None and media.pvrid:
            data.channel = data.programme = data.next_programme = None
            if data.recording is None or data.recording.pvrid != media.pvrid:
                try:
                    data.recording = await self.box.get_recording(media.pvrid)
                except SkyQError as err:
                    _LOGGER.debug("Recording %s not found: %s", media.pvrid, err)
                    data.recording = None
            try:
                data.position = await self.box.get_position_info()
                data.position_at = datetime.now(UTC)
            except SkyQError:
                data.position = data.position_at = None
        else:  # an app, or nothing
            data.channel = data.programme = data.next_programme = data.recording = data.position = None

    async def _refresh_programme(self, data: SkyQData, sid: int, now: datetime) -> None:
        try:
            data.programme, data.next_programme = await self.box.epg.now_and_next(data.channel or sid, now)
        except SkyQError as err:
            _LOGGER.debug("No guide data for sid %s: %s", sid, err)
            data.programme = data.next_programme = None

    # ------------------------------------------------------------------ push

    async def async_start_push(self) -> None:
        self._unsubscribe = [
            self.box.subscribe_system_status(self._on_system_status),
            self.box.subscribe_apps(self._on_apps),
            self.box.subscribe_pvr(self._on_pvr),
            self.box.subscribe_channel_list(self._on_channel_list),
        ]

    @callback
    def _push(self, **changes: object) -> None:
        if self.data is not None:
            self.async_set_updated_data(replace(self.data, **changes))  # type: ignore[arg-type]

    @callback
    def _request_refresh(self) -> None:
        self.config_entry.async_create_background_task(
            self.hass, self.async_request_refresh(), f"{DOMAIN} refresh {self.host}"
        )

    @callback
    def _on_system_status(self, status: SystemStatus) -> None:
        previous = self.data.system_status if self.data else None
        self._push(system_status=status)
        if previous is not None and previous.system_state != status.system_state:
            self._request_refresh()  # standby <-> active: poll now rather than in 30 s

    @callback
    def _on_apps(self, apps: AppsStatus) -> None:
        previous = self.data.visible_app if self.data else None
        self._push(visible_app=apps.visible_app)
        if previous != apps.visible_app:
            self._request_refresh()  # leaving or entering an app changes what's playing

    @callback
    def _on_pvr(self, status: PvrStatus) -> None:
        if self.fetches_pvr:
            self.mark_stale("pvr")
            self._request_refresh()

    @callback
    def _on_channel_list(self, _data: dict[str, object]) -> None:
        self.mark_stale("catalogue")
        self._request_refresh()

    async def async_shutdown(self) -> None:
        """Stop push streams and polling (also called automatically on unload)."""
        for unsubscribe in self._unsubscribe:
            unsubscribe()
        self._unsubscribe.clear()
        await self.box.close()
        await super().async_shutdown()
