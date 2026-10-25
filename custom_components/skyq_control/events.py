"""Box events, worked out by comparing one update with the next.

Each event is fired on the bus as `skyq_control_event` with the box's `device_id` and a `type`, which is
what the device triggers listen for, and is passed to the box's event entities.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any

from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback

from .aioskyq import Recording, RecordingStatus
from .const import DOMAIN
from .coordinator import SkyQCoordinator, SkyQData
from .entity import channel_label, visible_app

EVENT_TYPE = f"{DOMAIN}_event"

CHANNEL_CHANGED = "channel_changed"
APP_CHANGED = "app_changed"
RECORDING_STARTED = "recording_started"
RECORDING_FINISHED = "recording_finished"
RECORDING_FAILED = "recording_failed"

PLAYBACK_EVENTS = (CHANNEL_CHANGED, APP_CHANGED)
RECORDING_EVENTS = (RECORDING_STARTED, RECORDING_FINISHED, RECORDING_FAILED)

_MADE = (RecordingStatus.RECORDED, RecordingStatus.PART_RECORDED)


def box_events(old: SkyQData, new: SkyQData, *, recordings: bool) -> Iterator[tuple[str, dict[str, Any]]]:
    """Events between two updates. `recordings`: this box holds the household's recordings."""
    if new.is_on:
        yield from _playback_events(old, new)
    if recordings and old.recordings is not None and new.recordings is not None:
        yield from _recording_events(old, new)


def _playback_events(old: SkyQData, new: SkyQData) -> Iterator[tuple[str, dict[str, Any]]]:
    channel = new.channel
    if channel is not None and (old.channel is None or old.channel.sid != channel.sid or not old.is_on):
        yield (
            CHANNEL_CHANGED,
            {
                "channel": channel_label(channel),
                "channel_number": channel.number,
                "service_id": channel.sid,
                "programme": new.programme.title if new.programme else None,
            },
        )
    before, after = visible_app(old) if old.is_on else None, visible_app(new)
    if (before.app_id if before else None) != (after.app_id if after else None):
        # Leaving an app for live TV is an app change too, with no app.
        yield APP_CHANGED, {"app_id": after.app_id if after else None, "app_name": after.title if after else None}


def _recording_events(old: SkyQData, new: SkyQData) -> Iterator[tuple[str, dict[str, Any]]]:
    assert old.recordings is not None and new.recordings is not None
    previous = {r.pvrid: r.status for r in old.recordings.recordings if not r.is_deleted}
    for recording in new.recordings.recordings:
        if recording.is_deleted:
            continue
        before, now = previous.get(recording.pvrid), recording.status
        if now is RecordingStatus.RECORDING and before is not RecordingStatus.RECORDING:
            yield RECORDING_STARTED, _details(recording)
        elif now in _MADE and before in (RecordingStatus.RECORDING, RecordingStatus.SCHEDULED):
            yield RECORDING_FINISHED, _details(recording)
        elif now is RecordingStatus.FAILED and before is not RecordingStatus.FAILED:
            yield RECORDING_FAILED, _details(recording)


def _details(recording: Recording) -> dict[str, Any]:
    return {
        "recording_id": recording.pvrid,
        "title": recording.title,
        "channel": recording.channel_name,
        "status": recording.status_raw,
        "failure_reason": recording.failure_reason if recording.failure_reason != "N/A" else None,
    }


class BoxEvents:
    """Watches one box's coordinator and fires its events on the bus and to its event entities."""

    def __init__(self, hass: HomeAssistant, coordinator: SkyQCoordinator, device_id: str) -> None:
        self._hass = hass
        self._coordinator = coordinator
        self.device_id = device_id
        self._last = coordinator.data  # nothing is fired for what was already true at setup
        self._listeners: list[Callable[[str, dict[str, Any]], None]] = []

    @callback
    def async_start(self) -> CALLBACK_TYPE:
        return self._coordinator.async_add_listener(self._on_update)

    @callback
    def async_add_listener(self, listener: Callable[[str, dict[str, Any]], None]) -> CALLBACK_TYPE:
        self._listeners.append(listener)
        return lambda: self._listeners.remove(listener)

    @callback
    def _on_update(self) -> None:
        old, new = self._last, self._coordinator.data
        self._last = new
        if old is None or new is None or old is new:
            return
        for event_type, data in box_events(old, new, recordings=self._coordinator.fetches_pvr):
            self._hass.bus.async_fire(EVENT_TYPE, {"device_id": self.device_id, "type": event_type, **data})
            for listener in list(self._listeners):
                listener(event_type, data)
