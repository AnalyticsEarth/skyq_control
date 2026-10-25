# Bundled copy of aioskyq; edit aioskyq/src/aioskyq and run scripts/sync_aioskyq.py.
"""High-level client for one Sky Q box."""

from __future__ import annotations

import asyncio
import contextlib
import functools
import logging
from collections.abc import Awaitable, Callable, Iterable
from datetime import UTC, date, datetime
from typing import Any, TypeVar

import aiohttp

from .const import (
    REMOTE_PORT,
    REST_PORT,
    TERRITORIES,
    UNSUPPORTED_DEVICE_TYPES,
    UPNP_PORT,
    WS_APPS_STATUS,
    WS_PVR_STATUS,
    WS_SERVICES_STATUS,
    WS_SYSTEM_STATUS,
    Key,
    PowerState,
    RecordingStatus,
)
from .epg import EpgClient
from .exceptions import (
    SkyQConnectionError,
    SkyQError,
    SkyQNotFoundError,
    SkyQNotSupportedError,
    SkyQUnsupportedDeviceError,
)
from .models import (
    App,
    AppsStatus,
    Channel,
    ChannelDetails,
    DeviceInfo,
    EpgEvent,
    Favourite,
    Genre,
    MediaInfo,
    NowPlaying,
    PinStatus,
    PositionInfo,
    PvrStatus,
    Recording,
    RecordingList,
    Storage,
    SystemStatus,
    SystemTime,
    TransportInfo,
)
from .remote import DEFAULT_KEY_GAP, KeyLike, RemoteClient
from .rest import RestClient
from .soap import SoapClient
from .ws import ConnectionCallback, StatusStream

_LOGGER = logging.getLogger(__name__)

T = TypeVar("T")
Listener = Callable[[T], Awaitable[None] | None]

PVR_PAGE_SIZE = 1000
BOOKING_LOOKUP_ATTEMPTS = 5


class SkyQBox:
    """One Sky Q gateway or Mini box.

    Reads go over REST (9006) and SOAP (49153); control goes over remote keys (49160)
    because Sky Q locks SOAP transport control; live state arrives on websockets.
    """

    def __init__(
        self,
        host: str,
        session: aiohttp.ClientSession | None = None,
        *,
        rest_port: int = REST_PORT,
        upnp_port: int = UPNP_PORT,
        remote_port: int = REMOTE_PORT,
        key_gap: float = DEFAULT_KEY_GAP,
        territory: str | None = None,
        epg_client: EpgClient | None = None,
    ) -> None:
        self.host = host
        self._session = session or aiohttp.ClientSession()
        self._owns_session = session is None
        self.rest = RestClient(self._session, host, rest_port)
        self.soap = SoapClient(self._session, host, upnp_port)
        self.remote = RemoteClient(host, remote_port, key_gap=key_gap)
        self._territory = territory
        self._epg = epg_client
        self._streams: dict[str, StatusStream] = {}
        self._listeners: dict[str, list[Callable[[dict[str, Any]], Awaitable[None] | None]]] = {}
        self._connection_listeners: list[Callable[[str, bool], Awaitable[None] | None]] = []
        self._background: set[asyncio.Future[None]] = set()
        self.device_info: DeviceInfo | None = None
        self._channels: dict[int, Channel] | None = None

    async def __aenter__(self) -> SkyQBox:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def close(self) -> None:
        await asyncio.gather(*(stream.stop() for stream in self._streams.values()))
        self._streams.clear()
        if self._owns_session:
            await self._session.close()

    # ------------------------------------------------------------------ device

    async def get_device_info(self, *, refresh: bool = False) -> DeviceInfo:
        if self.device_info and not refresh:
            return self.device_info
        device, system = await asyncio.gather(
            self.rest.get("system/deviceinformation"), self.rest.get("system/information")
        )
        info = DeviceInfo.from_api(self.host, device or {}, system or {})
        if info.device_type in UNSUPPORTED_DEVICE_TYPES:
            raise SkyQUnsupportedDeviceError(f"{self.host} is a {info.device_type}, not a Sky Q set-top box")
        self.device_info = info
        return info

    async def get_power_state(self) -> PowerState:
        """ON, STANDBY, or OFF when the box does not answer at all."""
        try:
            data = await self.rest.get("system/information", timeout=4)
        except SkyQConnectionError:
            return PowerState.OFF
        return PowerState.STANDBY if (data or {}).get("activeStandby") else PowerState.ON

    async def get_system_time(self) -> SystemTime:
        return SystemTime.from_api(await self.rest.get("system/time"))

    async def ping(self) -> bool:
        try:
            await self.rest.post("system/action/ping", timeout=4)
        except SkyQError:
            return False
        return True

    # ------------------------------------------------------------------ channels & apps

    async def get_channels(self, *, refresh: bool = False) -> list[Channel]:
        if self._channels is not None and not refresh:
            return list(self._channels.values())
        info = await self.get_device_info()
        data = None
        if info.bouquet is not None and info.subbouquet is not None:
            with contextlib.suppress(SkyQNotFoundError, SkyQNotSupportedError):
                data = await self.rest.get(f"services/{info.bouquet}/{info.subbouquet}")
        if data is None:
            data = await self.rest.get("services")
        channels = [Channel.from_api(item) for item in (data or {}).get("services") or ()]
        self._channels = {channel.sid: channel for channel in channels}
        return channels

    async def get_channel(self, sid: int) -> Channel | None:
        if self._channels is None:
            await self.get_channels()
        assert self._channels is not None
        return self._channels.get(sid)

    async def get_channel_by_number(self, number: str | int) -> Channel | None:
        return next((c for c in await self.get_channels() if c.number == str(number)), None)

    async def get_channel_details(self, sid: int) -> ChannelDetails:
        return ChannelDetails.from_api(await self.rest.get(f"services/details/{sid}"))

    async def get_genres(self) -> list[Genre]:
        return [Genre.from_api(g) for g in (await self.rest.get("services/genres") or {}).get("genres") or ()]

    async def get_favourites(self) -> list[Favourite]:
        data = await self.rest.get("services/favourites") or {}
        return sorted((Favourite.from_api(f) for f in data.get("favourites") or ()), key=lambda f: f.position)

    async def get_apps(self) -> list[App]:
        return [App.from_api(a) for a in (await self.rest.get("apps") or {}).get("apps") or ()]

    async def launch_app(self, app_id: str) -> None:
        """Open an installed app by its appId (e.g. "YouTube", "Netflix")."""
        await self.rest.post("apps/action/launch", {"appId": app_id})

    # ------------------------------------------------------------------ PVR (reads)

    async def get_recordings(self) -> RecordingList:
        """Every PVR item (paged transparently)."""
        first = await self.rest.get("pvr", {"limit": PVR_PAGE_SIZE, "offset": 0}, timeout=30) or {}
        items = list(first.get("pvrItems") or ())
        total = first.get("totalPvrItems") or len(items)
        while len(items) < total:
            page = await self.rest.get("pvr", {"limit": PVR_PAGE_SIZE, "offset": len(items)}, timeout=30) or {}
            batch = page.get("pvrItems") or []
            if not batch:
                break
            items.extend(batch)
        return RecordingList.from_api({**first, "pvrItems": items})

    async def get_recording(self, pvrid: str) -> Recording:
        data = await self.rest.get(f"pvr/details/{pvrid}") or {}
        if "details" not in data:
            raise SkyQNotFoundError(404, None, f"Recording {pvrid} not found")
        return Recording.from_api(data["details"])

    async def get_recording_changes(self, document_id: str, version: int) -> RecordingList:
        """Items changed since `version` (use the numbers from get_recordings or a PvrStatus push)."""
        return RecordingList.from_api(await self.rest.get(f"pvr/changes/{document_id}/{version}") or {})

    async def get_storage(self) -> Storage:
        return Storage.from_api(await self.rest.get("pvr/storage"))

    async def get_pin_status(self) -> PinStatus:
        return PinStatus.from_api(await self.rest.get("pin") or {})

    # ------------------------------------------------------------------ PVR (actions)

    async def book_recording(self, eid: str, *, series: bool = False) -> Recording | None:
        """Book an EPG event (`EpgEvent.eid`). Returns the new SCHEDULED item once it appears.

        The box answers with an empty body, so the booking is found by its `oeid`.
        """
        action = "bookseriesrecording" if series else "bookrecording"
        await self.rest.post(f"pvr/action/{action}", {"eid": eid})
        return await self._find_booking(eid)

    async def book_ppv_recording(self, eid: str, offer_ref: str, pin: str | None = None) -> Recording | None:
        """Book a pay-per-view event. This spends money; never call it without the user's explicit ask."""
        params = {"eid": eid, "offerref": offer_ref}
        if pin is not None:
            params["pin"] = pin
        await self.rest.post("pvr/action/bookppvrecording", params)
        return await self._find_booking(eid)

    async def _find_booking(self, eid: str) -> Recording | None:
        for attempt in range(BOOKING_LOOKUP_ATTEMPTS):
            if attempt:
                await asyncio.sleep(1)
            for recording in (await self.get_recordings()).recordings:
                if recording.eid == eid and recording.status is RecordingStatus.SCHEDULED:
                    return recording
        return None

    async def cancel_booking(self, pvrid: str) -> None:
        """Remove a SCHEDULED booking (the box applies it a few seconds later)."""
        await self.rest.post("pvr/action/delete", {"pvrid": pvrid})

    async def stop_recording(self, pvrid: str) -> None:
        """Stop an in-progress recording, keeping what was recorded so far."""
        await self.rest.post("pvr/action/cancelrecording", {"pvrid": pvrid})

    async def delete_recording(self, pvrid: str) -> None:
        """Move a recording to the trash (reversible with undelete_recording)."""
        await self.rest.post("pvr/action/delete", {"pvrid": pvrid})

    async def undelete_recording(self, pvrid: str) -> None:
        await self.rest.post("pvr/action/undelete", {"pvrid": pvrid})

    async def erase_recording(self, pvrid: str) -> None:
        """Permanently erase one recording. Not reversible."""
        await self.rest.post("pvr/action/erase", {"pvrid": pvrid})

    async def erase_all_recordings(self, *, confirm: bool = False) -> None:
        """Permanently erase the whole PVR. Requires confirm=True."""
        if not confirm:
            raise ValueError("erase_all_recordings permanently deletes every recording; pass confirm=True")
        await self.rest.delete("pvr")

    async def set_keep(self, pvrid: str, keep: bool = True) -> None:
        await self.rest.post(f"pvr/action/{'keep' if keep else 'unkeep'}", {"pvrid": pvrid})

    async def set_lock(self, pvrid: str, locked: bool = True) -> None:
        await self.rest.post(f"pvr/action/{'lock' if locked else 'unlock'}", {"pvrid": pvrid})

    async def set_series_link(self, pvrid: str, linked: bool = True) -> None:
        await self.rest.post(f"pvr/action/{'serieslink' if linked else 'seriesunlink'}", {"pvrid": pvrid})

    async def set_resume_position(self, pvrid: str, seconds: int) -> None:
        """Move the resume point forward. Moving it backwards makes the box return 500 and then
        hang, so earlier positions are rejected here."""
        current = (await self.get_recording(pvrid)).resume_position or 0
        if seconds < current:
            raise ValueError(f"Sky Q only moves the resume position forward ({seconds}s < current {current}s)")
        await self.rest.post("pvr/action/setlastplayedposition", {"pos": seconds, "pvrid": pvrid})

    # ------------------------------------------------------------------ playback state

    async def get_transport_info(self) -> TransportInfo:
        return await self.soap.get_transport_info()

    async def get_media_info(self) -> MediaInfo:
        return await self.soap.get_media_info()

    async def get_position_info(self) -> PositionInfo:
        return await self.soap.get_position_info()

    async def get_now_playing(self, *, include_programme: bool = True) -> NowPlaying:
        transport, media = await asyncio.gather(self.get_transport_info(), self.get_media_info())
        channel = programme = recording = position = None
        if media.sid is not None:
            channel = await self.get_channel(media.sid)
            if include_programme:
                try:
                    programme, _ = await self.epg.now_and_next(channel or media.sid)
                except SkyQError as err:
                    _LOGGER.debug("No EPG for %s: %s", media.sid, err)
        elif media.pvrid:
            with contextlib.suppress(SkyQError):
                recording = await self.get_recording(media.pvrid)
            with contextlib.suppress(SkyQError):
                position = await self.get_position_info()
        return NowPlaying(
            transport=transport,
            media=media,
            channel=channel,
            programme=programme,
            recording=recording,
            position=position,
        )

    # ------------------------------------------------------------------ control (remote keys)

    async def press(self, *keys: KeyLike | Iterable[KeyLike]) -> None:
        await self.remote.press(*keys)

    async def change_channel(self, number: str | int, *, dismiss_first: bool = True) -> None:
        """Tune by typing the channel number (SOAP tuning is locked on Sky Q)."""
        digits = str(number).strip()
        if not digits.isdigit():
            raise ValueError(f"Channel number must be digits, got {number!r}")
        keys: list[KeyLike] = [Key.BACKUP] if dismiss_first else []
        keys.extend(digits)
        await self.press(keys)

    async def tune(self, sid: int) -> None:
        channel = await self.get_channel(sid)
        if channel is None:
            raise ValueError(f"Unknown channel sid {sid}")
        await self.change_channel(channel.number)

    async def turn_on(self) -> None:
        if await self.get_power_state() is PowerState.STANDBY:
            await self.press(Key.POWER)

    async def turn_off(self) -> None:
        if await self.get_power_state() is PowerState.ON:
            await self.press(Key.POWER)

    async def play(self) -> None:
        await self.press(Key.PLAY)

    async def pause(self) -> None:
        await self.press(Key.PAUSE)

    async def stop(self) -> None:
        await self.press(Key.STOP)

    async def fast_forward(self) -> None:
        await self.press(Key.FAST_FORWARD)

    async def rewind(self) -> None:
        await self.press(Key.REWIND)

    async def record(self) -> None:
        await self.press(Key.RECORD)

    # ------------------------------------------------------------------ EPG

    @property
    def epg(self) -> EpgClient:
        if self._epg is None:
            territory = self._territory
            if territory is None and self.device_info and self.device_info.country_code:
                territory = TERRITORIES.get(self.device_info.country_code.upper())
            self._epg = EpgClient(self._session, territory or "GB")
        return self._epg

    async def get_schedule(
        self, channels: Iterable[int | Channel], day: date | None = None
    ) -> dict[int, list[EpgEvent]]:
        return await self.epg.get_day(channels, day or datetime.now(UTC).date())

    # ------------------------------------------------------------------ push updates

    def subscribe_system_status(self, listener: Listener[SystemStatus]) -> Callable[[], None]:
        return self._subscribe(WS_SYSTEM_STATUS, lambda data: listener(SystemStatus.from_api(data)))

    def subscribe_apps(self, listener: Listener[AppsStatus]) -> Callable[[], None]:
        return self._subscribe(WS_APPS_STATUS, lambda data: listener(AppsStatus.from_api(data)))

    def subscribe_pvr(self, listener: Listener[PvrStatus]) -> Callable[[], None]:
        """Fires when the PVR changes; call get_recording_changes() for the details."""
        return self._subscribe(WS_PVR_STATUS, lambda data: listener(PvrStatus.from_api(data)))

    def subscribe_channel_list(self, listener: Listener[dict[str, Any]]) -> Callable[[], None]:
        """Fires when the channel list document changes (payload: {"documentId": …})."""

        def handle(data: dict[str, Any]) -> Awaitable[None] | None:
            self._channels = None  # refetch on next use
            return listener(data)

        return self._subscribe(WS_SERVICES_STATUS, handle)

    def subscribe_connection(self, listener: Callable[[str, bool], Awaitable[None] | None]) -> Callable[[], None]:
        """listener(stream_path, connected) whenever a push stream connects or drops."""
        self._connection_listeners.append(listener)
        return lambda: self._connection_listeners.remove(listener)

    @property
    def streams_connected(self) -> dict[str, bool]:
        return {path: stream.connected for path, stream in self._streams.items()}

    def _subscribe(self, path: str, handler: Callable[[dict[str, Any]], Awaitable[None] | None]) -> Callable[[], None]:
        self._listeners.setdefault(path, []).append(handler)
        stream = self._streams.get(path)
        if stream is None:
            stream = StatusStream(
                self._session,
                self.host,
                path,
                functools.partial(self._fan_out, path),
                port=self.rest.port,
                on_connection_change=self._connection_cb(path),
            )
            self._streams[path] = stream
        stream.start()

        def unsubscribe() -> None:
            listeners = self._listeners.get(path, [])
            if handler in listeners:
                listeners.remove(handler)
            if not listeners and (s := self._streams.pop(path, None)):
                task = asyncio.ensure_future(s.stop())
                self._background.add(task)
                task.add_done_callback(self._background.discard)

        return unsubscribe

    async def _fan_out(self, path: str, data: dict[str, Any]) -> None:
        for handler in list(self._listeners.get(path, ())):
            try:
                result = handler(data)
                if asyncio.iscoroutine(result) or isinstance(result, asyncio.Future):
                    await result
            except Exception:
                _LOGGER.exception("Error in %s listener", path)

    def _connection_cb(self, path: str) -> ConnectionCallback:
        async def changed(connected: bool) -> None:
            for listener in list(self._connection_listeners):
                result = listener(path, connected)
                if asyncio.iscoroutine(result):
                    await result

        return changed
