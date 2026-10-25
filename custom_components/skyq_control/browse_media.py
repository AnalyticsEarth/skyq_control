"""Media browser: channels grouped like Sky's guide, what's on each, and a channel's schedule."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable
from datetime import datetime, timedelta

from homeassistant.components.media_player import BrowseError, BrowseMedia, MediaClass, MediaType
from homeassistant.util import dt as dt_util

from .aioskyq import Channel, EpgClient, EpgEvent, SkyQError
from .const import DOMAIN, GUIDE_BROWSE_HOURS
from .coordinator import SkyQData
from .entity import channel_label, favourite_channels, source_channels, user_apps

_LOGGER = logging.getLogger(__name__)

FAVOURITES = "favourites"
APPS = "apps"
GENRE_PREFIX = "genre:"
SID_PREFIX = "sid:"
MEDIA_TYPE_PROGRAMME = "programme"
# Folder listings show what's on now when the guide answers quickly; otherwise they show channels only.
NOW_ON_TIMEOUT = 10.0


def genre_folders(data: SkyQData) -> dict[int, list[Channel]]:
    """Channels by genre, folders ordered by their lowest channel (Entertainment 101, Movies 301, ...)."""
    folders: dict[int, list[Channel]] = {}
    for channel in source_channels(data):
        folders.setdefault(channel.genre or 0, []).append(channel)
    return folders


async def browse(data: SkyQData, guide: EpgClient, content_type: str | None, content_id: str | None) -> BrowseMedia:
    if not content_id:
        return _root(data)
    if content_id == FAVOURITES:
        return await _channel_folder(guide, FAVOURITES, "Favourites", favourite_channels(data))
    if content_id == APPS:
        return _apps(data)
    if (genre := _number_after(content_id, GENRE_PREFIX)) is not None and (channels := genre_folders(data).get(genre)):
        return await _channel_folder(guide, content_id, _genre_title(data, genre), channels)
    if (sid := _number_after(content_id, SID_PREFIX)) is not None and (channel := data.channels.get(sid)):
        return await _schedule(guide, channel)
    raise BrowseError(
        translation_domain=DOMAIN, translation_key="unknown_media", translation_placeholders={"media_id": content_id}
    )


def _number_after(content_id: str, prefix: str) -> int | None:
    rest = content_id.removeprefix(prefix)
    return int(rest) if rest != content_id and rest.isdigit() else None


def _root(data: SkyQData) -> BrowseMedia:
    children = []
    if favourite_channels(data):
        children.append(_folder(FAVOURITES, "Favourites"))
    children.extend(_folder(f"{GENRE_PREFIX}{genre}", _genre_title(data, genre)) for genre in genre_folders(data))
    if user_apps(data):
        children.append(_folder(APPS, "Apps"))
    return BrowseMedia(
        media_class=MediaClass.DIRECTORY,
        media_content_id="",
        media_content_type="",
        title="Sky Q",
        can_play=False,
        can_expand=True,
        children=children,
        children_media_class=MediaClass.DIRECTORY,
    )


def _apps(data: SkyQData) -> BrowseMedia:
    return BrowseMedia(
        media_class=MediaClass.DIRECTORY,
        media_content_id=APPS,
        media_content_type=APPS,
        title="Apps",
        can_play=False,
        can_expand=True,
        children=[
            BrowseMedia(
                media_class=MediaClass.APP,
                media_content_id=app.app_id,
                media_content_type=MediaType.APP,
                title=app.title,
                can_play=True,
                can_expand=False,
            )
            for app in user_apps(data)
        ],
        children_media_class=MediaClass.APP,
    )


def _genre_title(data: SkyQData, genre: int) -> str:
    return data.genres.get(genre) or "Other"


def _folder(content_id: str, title: str) -> BrowseMedia:
    return BrowseMedia(
        media_class=MediaClass.DIRECTORY,
        media_content_id=content_id,
        media_content_type=content_id,
        title=title,
        can_play=False,
        can_expand=True,
    )


async def _channel_folder(guide: EpgClient, content_id: str, title: str, channels: list[Channel]) -> BrowseMedia:
    now_on = await _now_on(guide, channels)
    return BrowseMedia(
        media_class=MediaClass.DIRECTORY,
        media_content_id=content_id,
        media_content_type=content_id,
        title=title,
        can_play=False,
        can_expand=True,
        children=[_channel_item(channel, now_on.get(channel.sid)) for channel in channels],
        children_media_class=MediaClass.CHANNEL,
    )


async def _now_on(guide: EpgClient, channels: Iterable[Channel]) -> dict[int, EpgEvent]:
    listed = [channel for channel in channels if channel.has_schedule]
    now = dt_util.utcnow()
    try:
        async with asyncio.timeout(NOW_ON_TIMEOUT):
            schedule = await guide.get_schedule(listed, now, now + timedelta(seconds=1))
    except (SkyQError, TimeoutError) as err:
        _LOGGER.debug("No guide data for the media browser: %s", err)
        return {}
    return {sid: events[0] for sid, events in schedule.items() if events}


def _channel_item(channel: Channel, now_on: EpgEvent | None = None) -> BrowseMedia:
    title = channel_label(channel)
    if now_on:
        title = f"{title} · {now_on.title}"
    return BrowseMedia(
        media_class=MediaClass.CHANNEL,
        media_content_id=f"{SID_PREFIX}{channel.sid}",
        media_content_type=MediaType.CHANNEL,
        title=title,
        can_play=True,
        can_expand=channel.has_schedule,
        thumbnail=channel.logo_url,
    )


async def _schedule(guide: EpgClient, channel: Channel) -> BrowseMedia:
    """The channel's programmes from now for the next day. Only the one on now can be played (it tunes)."""
    now = dt_util.utcnow()
    try:
        events = await guide.get_events(channel, now, now + timedelta(hours=GUIDE_BROWSE_HOURS))
    except SkyQError as err:
        _LOGGER.debug("No guide data for %s: %s", channel_label(channel), err)
        events = []
    item = _channel_item(channel)
    item.children = [_programme_item(channel, event, now) for event in events]
    item.children_media_class = MediaClass.VIDEO
    return item


def _programme_item(channel: Channel, event: EpgEvent, now: datetime) -> BrowseMedia:
    on_now = event.is_on_at(now)
    return BrowseMedia(
        media_class=MediaClass.EPISODE if event.episode else MediaClass.VIDEO,
        media_content_id=f"{SID_PREFIX}{channel.sid}" if on_now else f"event:{event.eid}",
        media_content_type=MediaType.CHANNEL if on_now else MEDIA_TYPE_PROGRAMME,
        title=f"{'Now' if on_now else format_start(event.start, now)} · {event.title}",
        can_play=on_now,
        can_expand=False,
        thumbnail=event.image_url or channel.logo_url,
    )


def format_start(start: datetime, now: datetime) -> str:
    """ "21:00" today, "Sat 21:00" on another day, in Home Assistant's time zone."""
    local = dt_util.as_local(start)
    if local.date() == dt_util.as_local(now).date():
        return local.strftime("%H:%M")
    return local.strftime("%a %H:%M")
