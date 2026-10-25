"""Actions. Each targets a Sky Q media player, and that player's box carries it out.

Recording actions work from any box: a Mini passes them on to its gateway, which holds the recordings.
Two of the box's PVR calls are deliberately not exposed: erasing every recording at once, and booking
pay-per-view events (which spends money).
"""

from __future__ import annotations

from collections.abc import Awaitable
from typing import TYPE_CHECKING, Any

import voluptuous as vol
from homeassistant.components.media_player import DOMAIN as MEDIA_PLAYER_DOMAIN
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.service import async_register_platform_entity_service
from homeassistant.util import dt as dt_util

from .aioskyq import Recording, RecordingStatus
from .const import DOMAIN, GUIDE_MAX_HOURS
from .entity import favourite_channels, household_recordings, source_channels, user_apps

if TYPE_CHECKING:
    from .media_player import SkyQMediaPlayer

ATTR_RECORDING_ID = "recording_id"
STATUS_FILTERS = {
    "recorded": (RecordingStatus.RECORDED, RecordingStatus.PART_RECORDED),
    "scheduled": (RecordingStatus.SCHEDULED,),
    "recording": (RecordingStatus.RECORDING,),
    "failed": (RecordingStatus.FAILED,),
}
DELETED = "deleted"


def _comma_list(value: object) -> list[str]:
    """Accept a list or comma-separated text ("backup, 1, 0, 1")."""
    return [key.strip() for item in cv.ensure_list(value) for key in str(item).split(",") if key.strip()]


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    def register(
        name: str, schema: dict[Any, Any], func: Any, response: SupportsResponse = SupportsResponse.NONE
    ) -> None:
        async_register_platform_entity_service(
            hass,
            DOMAIN,
            name,
            entity_domain=MEDIA_PLAYER_DOMAIN,
            schema=schema,
            func=func,
            supports_response=response,
        )

    # Remote and playback
    register(
        "send_keys",
        {
            vol.Required("keys"): vol.All(_comma_list, vol.Length(min=1)),
            vol.Optional("delay"): vol.All(vol.Coerce(float), vol.Range(min=0, max=5)),
        },
        "async_send_keys",
    )
    register("change_channel", {vol.Required("channel"): cv.string}, "async_change_channel")
    register("launch_app", {vol.Required("app"): cv.string}, "async_launch_app")

    # Guide
    register(
        "get_guide",
        {
            vol.Optional("channels"): vol.All(_comma_list, [cv.string]),
            vol.Optional("start"): cv.datetime,
            vol.Optional("hours", default=6): vol.All(vol.Coerce(float), vol.Range(min=0.5, max=GUIDE_MAX_HOURS)),
            vol.Optional("search"): cv.string,
        },
        "async_get_guide",
        SupportsResponse.ONLY,
    )
    register(
        "record",
        {vol.Required("event_id"): cv.string, vol.Optional("series", default=False): cv.boolean},
        "async_record",
    )
    register("record_now", {vol.Optional("series", default=False): cv.boolean}, _record_now)

    # Recordings
    recording_id = {vol.Required(ATTR_RECORDING_ID): vol.All(cv.string, vol.Strip)}
    register("delete_recording", {**recording_id, vol.Optional("permanent", default=False): cv.boolean}, _delete)
    register("restore_recording", recording_id, _restore)
    register("stop_recording", recording_id, _stop)
    register(
        "update_recording",
        {
            **recording_id,
            vol.Optional("keep"): cv.boolean,
            vol.Optional("locked"): cv.boolean,
            vol.Optional("series_link"): cv.boolean,
            vol.Optional("resume_position"): vol.All(vol.Coerce(int), vol.Range(min=0)),
        },
        _update,
    )

    # Lookups
    register(
        "get_recordings",
        {
            vol.Optional("status"): vol.All(cv.ensure_list, [vol.In([*STATUS_FILTERS, DELETED])]),
            vol.Optional("search"): cv.string,
            vol.Optional("limit", default=100): vol.All(vol.Coerce(int), vol.Range(min=1, max=2000)),
        },
        _get_recordings,
        SupportsResponse.ONLY,
    )
    register("get_channels", {}, _get_channels, SupportsResponse.ONLY)
    register("get_apps", {}, _get_apps, SupportsResponse.ONLY)


# ------------------------------------------------------------------ recordings


def _find(player: SkyQMediaPlayer, recording_id: str) -> Recording | None:
    """The recording, None if the list isn't available here (the box decides), or an error if it's unknown."""
    items = household_recordings(player.coordinator)
    if items is None:
        return None
    if (match := next((r for r in items if r.pvrid == recording_id), None)) is None:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="unknown_recording",
            translation_placeholders={"recording_id": recording_id},
        )
    return match


async def _pvr_command(player: SkyQMediaPlayer, action: Awaitable[Any]) -> None:
    await player._command(action)
    if (pvr := player.coordinator.household_pvr) is not None and pvr is not player.coordinator:
        pvr.mark_stale("pvr")
        await pvr.async_request_refresh()


async def _record_now(player: SkyQMediaPlayer, call: ServiceCall) -> None:
    if (programme := player.data.programme) is None:
        raise ServiceValidationError(translation_domain=DOMAIN, translation_key="no_programme")
    await _pvr_command(player, player.coordinator.box.book_recording(programme.eid, series=call.data["series"]))


async def _delete(player: SkyQMediaPlayer, call: ServiceCall) -> None:
    recording_id = call.data[ATTR_RECORDING_ID]
    _find(player, recording_id)
    box = player.coordinator.box
    # Deleting a scheduled recording cancels the booking; deleted recordings sit in the trash until erased.
    action = box.erase_recording if call.data["permanent"] else box.delete_recording
    await _pvr_command(player, action(recording_id))


async def _restore(player: SkyQMediaPlayer, call: ServiceCall) -> None:
    recording_id = call.data[ATTR_RECORDING_ID]
    _find(player, recording_id)
    await _pvr_command(player, player.coordinator.box.undelete_recording(recording_id))


async def _stop(player: SkyQMediaPlayer, call: ServiceCall) -> None:
    recording_id = call.data[ATTR_RECORDING_ID]
    recording = _find(player, recording_id)
    if recording is not None and recording.status is not RecordingStatus.RECORDING:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="not_recording",
            translation_placeholders={"title": recording.title or recording_id},
        )
    await _pvr_command(player, player.coordinator.box.stop_recording(recording_id))


async def _update(player: SkyQMediaPlayer, call: ServiceCall) -> None:
    recording_id = call.data[ATTR_RECORDING_ID]
    changes = {key: call.data[key] for key in ("keep", "locked", "series_link", "resume_position") if key in call.data}
    if not changes:
        raise ServiceValidationError(translation_domain=DOMAIN, translation_key="nothing_to_update")
    recording = _find(player, recording_id)
    position = changes.get("resume_position")
    current = recording.resume_position if recording else None
    if position is not None and current is not None and position < current:
        # Moving it back makes the box return an error and then hang (seen on a real Mini).
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="resume_backwards",
            translation_placeholders={"position": str(position), "current": str(current)},
        )
    box = player.coordinator.box

    async def apply() -> None:
        if "keep" in changes:
            await box.set_keep(recording_id, changes["keep"])
        if "locked" in changes:
            await box.set_lock(recording_id, changes["locked"])
        if "series_link" in changes:
            await box.set_series_link(recording_id, changes["series_link"])
        if position is not None:
            await box.set_resume_position(recording_id, position)

    await _pvr_command(player, apply())


def _status_name(recording: Recording) -> str:
    if recording.is_deleted:
        return DELETED
    return next((name for name, statuses in STATUS_FILTERS.items() if recording.status in statuses), "unknown")


def recording_details(recording: Recording) -> dict[str, Any]:
    return {
        "recording_id": recording.pvrid,
        "title": recording.title,
        "status": _status_name(recording),
        "channel": recording.channel_name,
        "start": dt_util.as_local(recording.start).isoformat() if recording.start else None,
        "end": dt_util.as_local(recording.end).isoformat() if recording.end else None,
        "duration": recording.duration,
        "synopsis": recording.synopsis,
        "season": recording.season,
        "episode": recording.episode,
        "event_id": recording.eid,
        "keep": recording.keep,
        "locked": recording.locked,
        "series_linked": recording.series_linked,
        "resume_position": recording.resume_position,
        "failure_reason": recording.failure_reason,
        "uhd": recording.is_uhd,
        "image": recording.image_url,
    }


async def _get_recordings(player: SkyQMediaPlayer, call: ServiceCall) -> ServiceResponse:
    items = household_recordings(player.coordinator)
    if items is None:
        raise ServiceValidationError(translation_domain=DOMAIN, translation_key="no_recordings")
    wanted = set(call.data.get("status") or [*STATUS_FILTERS])
    needle = (call.data.get("search") or "").casefold().strip()
    matches = [r for r in items if _status_name(r) in wanted and (not needle or needle in (r.title or "").casefold())]
    # Bookings soonest first, then everything else newest first.
    upcoming = sorted((r for r in matches if r.status is RecordingStatus.SCHEDULED and not r.is_deleted), key=_start)
    others = sorted((r for r in matches if r not in upcoming), key=_start, reverse=True)
    found = [*upcoming, *others]
    return {
        "total": len(found),
        "recordings": [recording_details(r) for r in found[: call.data["limit"]]],
    }


def _start(recording: Recording) -> float:
    return recording.start.timestamp() if recording.start else 0.0


# ------------------------------------------------------------------ catalogue


async def _get_channels(player: SkyQMediaPlayer, call: ServiceCall) -> ServiceResponse:
    data = player.data
    favourites = {channel.sid: index for index, channel in enumerate(favourite_channels(data), start=1)}
    return {
        "channels": [
            {
                "number": channel.number,
                "name": channel.name,
                "service_id": channel.sid,
                "format": channel.format,
                "genre": data.genres.get(channel.genre or 0),
                "radio": channel.is_radio,
                "favourite": favourites.get(channel.sid),
                "logo": channel.logo_url,
            }
            for channel in source_channels(data)
        ]
    }


async def _get_apps(player: SkyQMediaPlayer, call: ServiceCall) -> ServiceResponse:
    return {"apps": [{"app_id": app.app_id, "title": app.title} for app in user_apps(player.data)]}
