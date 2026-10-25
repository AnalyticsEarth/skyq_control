"""Diagnostics for Sky Q Control (identifiers redacted)."""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from . import SkyQConfigEntry
from .const import TO_REDACT


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: SkyQConfigEntry) -> dict[str, Any]:
    coordinator = entry.runtime_data
    data = coordinator.data
    box = coordinator.box
    recordings = data.recordings.recordings if data.recordings else ()
    return async_redact_data(
        {
            "entry": {"data": dict(entry.data), "options": dict(entry.options), "unique_id": entry.unique_id},
            "coordinator": {
                "last_update_success": coordinator.last_update_success,
                "update_interval": str(coordinator.update_interval),
                "fetches_pvr": coordinator.fetches_pvr,
                "streams_connected": box.streams_connected,
            },
            "device": data.device.raw,
            "power": data.power.value,
            "boot_time": data.boot_time.isoformat() if data.boot_time else None,
            "playback": {
                "transport": asdict(data.transport) if data.transport else None,
                "media": asdict(data.media) if data.media else None,
                "channel": data.channel.raw if data.channel else None,
                "programme": data.programme.raw if data.programme else None,
                "recording_pvrid": data.recording.pvrid if data.recording else None,
                "position": asdict(data.position) if data.position else None,
                "visible_app": data.visible_app,
            },
            "system_status": data.system_status.raw if data.system_status else None,
            "upnp": [
                {
                    "device_type": d.device_type,
                    "model_number": d.model_number,
                    "index": d.description_index,
                    "services": [s.service_id for s in d.services],
                }
                for d in box.soap.devices
            ],
            "catalogue": {
                "channels": len(data.channels),
                "favourites": len(data.favourites),
                "apps": sorted(data.apps),
            },
            "pvr": {
                "total": len(recordings),
                "by_status": dict(Counter(r.status_raw for r in recordings)),
                "storage": asdict(data.storage) if data.storage else None,
                "pin": asdict(data.pin) if data.pin else None,
            },
        },
        TO_REDACT,
    )
