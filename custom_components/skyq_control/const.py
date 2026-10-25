"""Constants for the Sky Q Control integration."""

from __future__ import annotations

from datetime import timedelta
from typing import Final

DOMAIN: Final = "skyq_control"

CONF_KEY_GAP: Final = "key_gap"
CONF_SCAN_INTERVAL: Final = "scan_interval"

DEFAULT_KEY_GAP: Final = 0.5  # seconds between key presses; the box drops faster presses
DEFAULT_SCAN_INTERVAL: Final = 5  # seconds; channel changes are not pushed, so playback is polled
STANDBY_SCAN_INTERVAL: Final = timedelta(seconds=30)

# How often slower-changing data is refreshed (seconds). Push updates also force a refresh.
REFRESH_DEVICE: Final = 3600
REFRESH_CATALOGUE: Final = 3600  # channels, favourites, apps
REFRESH_PVR: Final = 900  # recordings, storage, PIN status

MANUAL_ENTRY: Final = "manual"

# Keys removed from diagnostics. Matches what tools/skyq_probe.py redacts.
TO_REDACT: Final = {
    "unique_id",
    "serialNumber",
    "serial_number",
    "MACAddress",
    "mac_address",
    "householdToken",
    "householdid",
    "viewingCardNumber",
    "deviceID",
    "receiverID",
    "vespaID",
    "airplayID",
    "btID",
    "tokens",
}
