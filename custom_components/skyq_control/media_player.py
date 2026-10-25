"""Media player for a Sky Q box."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Any

from homeassistant.components.media_player import (
    BrowseMedia,
    MediaPlayerDeviceClass,
    MediaPlayerEntity,
    MediaPlayerEntityFeature,
    MediaPlayerState,
    MediaType,
)
from homeassistant.core import HomeAssistant, ServiceResponse
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from . import SkyQConfigEntry
from .aioskyq import Channel, EpgEvent, Key, PowerState, SkyQError, TransportState
from .aioskyq.remote import resolve_key
from .browse_media import browse
from .const import DOMAIN
from .coordinator import SkyQCoordinator
from .entity import SkyQEntity, channel_label, household_recordings, source_channels, user_apps, visible_app

PARALLEL_UPDATES = 1  # one command at a time per box; the box drops overlapping key presses

MEDIA_TYPE_KEYS = "keys"


async def async_setup_entry(
    hass: HomeAssistant, entry: SkyQConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    async_add_entities([SkyQMediaPlayer(entry.runtime_data)])


class SkyQMediaPlayer(SkyQEntity, MediaPlayerEntity):
    _attr_name = None  # the device's name
    _attr_device_class = MediaPlayerDeviceClass.TV
    _attr_media_image_remotely_accessible = True  # Sky's public image hosts
    _attr_supported_features = (
        MediaPlayerEntityFeature.TURN_ON
        | MediaPlayerEntityFeature.TURN_OFF
        | MediaPlayerEntityFeature.PLAY
        | MediaPlayerEntityFeature.PAUSE
        | MediaPlayerEntityFeature.STOP
        | MediaPlayerEntityFeature.NEXT_TRACK
        | MediaPlayerEntityFeature.PREVIOUS_TRACK
        | MediaPlayerEntityFeature.SELECT_SOURCE
        | MediaPlayerEntityFeature.PLAY_MEDIA
        | MediaPlayerEntityFeature.BROWSE_MEDIA
    )

    def __init__(self, coordinator: SkyQCoordinator) -> None:
        super().__init__(coordinator, "media_player")

    # ------------------------------------------------------------------ state

    @property
    def state(self) -> MediaPlayerState:
        data = self.data
        if data.power is not PowerState.ON:
            return MediaPlayerState.OFF
        media = data.media
        if visible_app(data) and not (media and (media.is_live or media.is_recording)):
            return MediaPlayerState.ON
        transport = data.transport.state if data.transport else None
        if transport in (TransportState.PLAYING, TransportState.TRANSITIONING):
            return MediaPlayerState.PLAYING
        if transport is TransportState.PAUSED:
            return MediaPlayerState.PAUSED
        if transport in (TransportState.STOPPED, TransportState.NO_MEDIA):
            return MediaPlayerState.IDLE
        return MediaPlayerState.ON

    @property
    def media_content_type(self) -> MediaType | None:
        data = self.data
        if data.channel or (data.media and data.media.is_live):
            return MediaType.CHANNEL
        if data.recording:
            return MediaType.EPISODE if data.recording.episode else MediaType.MOVIE
        if visible_app(data):
            return MediaType.APP
        return None

    @property
    def media_content_id(self) -> str | None:
        data = self.data
        if data.media and data.media.sid is not None:
            return str(data.media.sid)
        if data.recording:
            return data.recording.pvrid
        app = visible_app(data)
        return app.app_id if app else None

    @property
    def media_title(self) -> str | None:
        data = self.data
        if data.programme:
            return data.programme.title
        if data.channel:
            return data.channel.name
        if data.recording:
            return data.recording.title
        app = visible_app(data)
        return app.title if app else None

    @property
    def media_channel(self) -> str | None:
        if channel := self.data.channel:
            return channel_label(channel)
        return self.data.recording.channel_name if self.data.recording else None

    @property
    def media_season(self) -> str | None:
        item = self.data.programme or self.data.recording
        return str(item.season) if item and item.season else None

    @property
    def media_episode(self) -> str | None:
        item = self.data.programme or self.data.recording
        return str(item.episode) if item and item.episode else None

    @property
    def media_image_url(self) -> str | None:
        data = self.data
        if data.programme and data.programme.image_url:
            return data.programme.image_url
        if data.channel:
            return data.channel.logo_url
        return data.recording.image_url if data.recording else None

    @property
    def media_duration(self) -> int | None:
        if self.data.programme:
            return self.data.programme.duration
        return self.data.recording.duration if self.data.recording else None

    @property
    def media_position(self) -> int | None:
        # Live TV: position 0 at the programme's start, so the progress bar advances on its own
        # without writing state on every poll.
        if self.data.programme:
            return 0
        position = self.data.position
        return position.position if self.data.recording and position else None

    @property
    def media_position_updated_at(self) -> datetime | None:
        if self.data.programme:
            return self.data.programme.start
        return self.data.position_at if self.data.recording else None

    @property
    def app_id(self) -> str | None:
        app = visible_app(self.data)
        return app.app_id if app else None

    @property
    def app_name(self) -> str | None:
        app = visible_app(self.data)
        return app.title if app else None

    @property
    def source_list(self) -> list[str]:
        return [channel_label(c) for c in source_channels(self.data)]

    @property
    def source(self) -> str | None:
        return channel_label(self.data.channel) if self.data.channel else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        data = self.data
        attributes: dict[str, Any] = {}
        if data.channel:
            attributes["channel_number"] = data.channel.number
            attributes["service_id"] = data.channel.sid
        if data.programme:
            attributes["programme_end"] = data.programme.end.isoformat()
            attributes["event_id"] = data.programme.eid
        if data.next_programme:
            attributes["next_programme"] = data.next_programme.title
            attributes["next_programme_start"] = data.next_programme.start.isoformat()
            attributes["next_event_id"] = data.next_programme.eid
        if data.recording:
            attributes["recording_id"] = data.recording.pvrid
        if data.transport and data.transport.speed not in (None, 1.0):
            attributes["playback_speed"] = data.transport.speed
        return attributes

    # ------------------------------------------------------------------ commands

    async def async_turn_on(self) -> None:
        await self._command(self.coordinator.box.turn_on())

    async def async_turn_off(self) -> None:
        await self._command(self.coordinator.box.turn_off())

    async def async_media_play(self) -> None:
        await self._command(self.coordinator.box.play())

    async def async_media_pause(self) -> None:
        await self._command(self.coordinator.box.pause())

    async def async_media_stop(self) -> None:
        await self._command(self.coordinator.box.stop())

    async def async_media_next_track(self) -> None:
        await self._command(self.coordinator.box.press(Key.CHANNEL_UP))

    async def async_media_previous_track(self) -> None:
        await self._command(self.coordinator.box.press(Key.CHANNEL_DOWN))

    async def async_select_source(self, source: str) -> None:
        channel = next((c for c in source_channels(self.data) if channel_label(c) == source), None)
        if channel is None:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="unknown_source", translation_placeholders={"source": source}
            )
        await self._command(self.coordinator.box.change_channel(channel.number))

    async def async_browse_media(
        self, media_content_type: MediaType | str | None = None, media_content_id: str | None = None
    ) -> BrowseMedia:
        return await browse(
            self.data,
            self.coordinator.box.epg,
            media_content_type,
            media_content_id,
            household_recordings(self.coordinator),
        )

    async def async_play_media(self, media_type: MediaType | str, media_id: str, **kwargs: Any) -> None:
        """channel: a number ("101") or "sid:3008"; app: an app id or title; keys: "up,up,select"."""
        if media_type == MediaType.CHANNEL:
            await self.async_change_channel(media_id)
        elif media_type == MediaType.APP:
            await self.async_launch_app(media_id)
        elif media_type == MEDIA_TYPE_KEYS:
            await self.async_send_keys([k for k in media_id.split(",") if k.strip()])
        else:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="unsupported_media_type",
                translation_placeholders={"media_type": str(media_type)},
            )

    # ------------------------------------------------------------------ entity actions (services)

    async def async_change_channel(self, channel: str) -> None:
        channel = str(channel).strip()
        if channel.startswith("sid:"):
            await self._command(self.coordinator.box.tune(int(channel[4:])))
            return
        if not channel.isdigit():
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="invalid_channel",
                translation_placeholders={"channel": channel},
            )
        await self._command(self.coordinator.box.change_channel(channel))

    async def async_launch_app(self, app: str) -> None:
        wanted = app.strip()
        match = next(
            (
                a
                for a in user_apps(self.data)
                if wanted in (a.app_id, a.title) or a.title.casefold() == wanted.casefold()
            ),
            None,
        )
        await self._command(self.coordinator.box.launch_app(match.app_id if match else wanted))

    async def async_send_keys(self, keys: list[str], delay: float | None = None) -> None:
        try:
            resolved = [resolve_key(k) for k in keys]
        except ValueError as err:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="unknown_key", translation_placeholders={"error": str(err)}
            ) from err
        box = self.coordinator.box
        if delay is None:
            await self._command(box.press(resolved))
            return

        async def press_with_delay() -> None:
            for index, key in enumerate(resolved):
                if index:
                    await asyncio.sleep(delay)
                await box.press(key)

        await self._command(press_with_delay())

    async def async_get_guide(
        self,
        channels: list[str] | None = None,
        start: datetime | None = None,
        hours: float = 6,
        search: str | None = None,
    ) -> ServiceResponse:
        """Programmes from the guide: on the given channels, else every channel when searching, else this
        box's current channel. Event IDs can be passed to the record action."""
        data = self.data
        if channels:
            wanted = list({c.sid: c for c in (self._find_channel(ref) for ref in channels)}.values())
        elif search:
            wanted = [c for c in source_channels(data) if c.has_schedule]
        elif data.channel:
            wanted = [data.channel]
        else:
            raise ServiceValidationError(translation_domain=DOMAIN, translation_key="no_channel")
        start = dt_util.as_utc(start) if start else dt_util.utcnow()
        try:
            schedule = await self.coordinator.box.epg.get_schedule(wanted, start, start + timedelta(hours=hours))
        except SkyQError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="guide_unavailable",
                translation_placeholders={"error": str(err)},
            ) from err
        needle = search.casefold().strip() if search else None
        programmes = [
            (channel, event)
            for channel in wanted
            for event in schedule.get(channel.sid, ())
            if needle is None or needle in event.title.casefold()
        ]
        programmes.sort(key=lambda item: (item[1].start, wanted.index(item[0])))
        return {"programmes": [_programme(channel, event) for channel, event in programmes]}

    async def async_record(self, event_id: str, series: bool = False) -> None:
        """Book a guide event (an event ID from get_guide or the media player's attributes)."""
        await self._command(self.coordinator.box.book_recording(event_id.strip(), series=series))

    def _find_channel(self, ref: str) -> Channel:
        """A channel by number ("101"), service ID ("sid:3008") or name, case-insensitively."""
        ref = str(ref).strip()
        channels = source_channels(self.data)
        if ref.startswith("sid:") and ref[4:].isdigit():
            match = self.data.channels.get(int(ref[4:]))
        elif ref.isdigit():
            match = next((c for c in channels if c.number == ref), None)
        else:
            match = next((c for c in channels if c.name.casefold() == ref.casefold()), None)
        if match is None:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="unknown_channel", translation_placeholders={"channel": ref}
            )
        return match


def _programme(channel: Channel, event: EpgEvent) -> dict[str, Any]:
    return {
        "channel": channel.name,
        "channel_number": channel.number,
        "service_id": channel.sid,
        "event_id": event.eid,
        "title": event.title,
        "start": dt_util.as_local(event.start).isoformat(),
        "end": dt_util.as_local(event.end).isoformat(),
        "synopsis": event.synopsis,
        "season": event.season,
        "episode": event.episode,
        "image": event.image_url,
        "new": event.new,
        "hd": event.hd,
        "subtitles": event.subtitles,
        "audio_description": event.audio_description,
        "rating": event.rating,
        "can_record": event.can_book,
        "can_series_link": event.can_series_link,
    }
