"""Constants for the Sky Q local API.

Everything here was confirmed against real boxes by tools/skyq_probe.py (see docs/PLAN.md §3).
"""

from __future__ import annotations

from enum import IntEnum, StrEnum

REST_PORT = 9006
UPNP_PORT = 49153
REMOTE_PORT = 49160
LEGACY_REMOTE_PORT = 5900  # firmware older than 060

UPNP_USER_AGENT = "SKYPLUS_skyplus"
SKY_PLAY_SERVICE = "urn:schemas-nds-com:service:SkyPlay:2"
SSDP_TARGETS = (
    "urn:schemas-nds-com:device:GatewaySkyControl:2",
    "urn:schemas-nds-com:device:MRSkyControl:2",
)
# UPnP description indexes to try; gateways use 1-5, Minis use 0-1.
DESCRIPTION_INDEXES = range(16)

# Live-status websockets that actually push data. The box accepts an upgrade on any
# `…/status` path, but only these send messages.
WS_SYSTEM_STATUS = "system/status"
WS_APPS_STATUS = "apps/status"
WS_PVR_STATUS = "pvr/status"
WS_SERVICES_STATUS = "services/status"

# Sky's public metadata hosts (no account needed).
HAWK_BASE = "https://awk.epgsky.com/hawk/linear"
ATLANTIS_BASE = "https://atlantis.epgsky.com/as"
HAWK_MAX_SIDS = 20
PROGRAMME_IMAGE_URL = "https://images.metadata.sky.com/pd-image/{uuid}/16-9/{width}"
CHANNEL_LOGO_URL = "https://d2n0069hmnqmmx.cloudfront.net/epgdata/1.0/newchanlogos/600/600/skychb{sid}.png"

# Country code reported by the box -> X-SkyOTT-Territory.
TERRITORIES = {"GBR": "GB", "IRL": "GB", "DEU": "DE", "ITA": "IT"}

API_ERROR_INVALID_URL = 101
API_ERROR_NOT_SUPPORTED = 104
UPNP_ERROR_TRANSPORT_LOCKED = 705


class Key(IntEnum):
    """Remote-control key codes sent over TCP 49160."""

    POWER = 0
    SELECT = 1
    BACKUP = 2
    CHANNEL_UP = 6
    CHANNEL_DOWN = 7
    INTERACTIVE = 8
    HELP = 9
    SERVICES = 10
    TV_GUIDE = 11
    RED_BUTTON = 12  # launches the channel's interactive service (2026-10-25 key sweep)
    I = 14  # noqa: E741 - the Sky remote's "i" button
    TEXT = 15
    UP = 16
    DOWN = 17
    LEFT = 18
    RIGHT = 19
    RED = 32
    GREEN = 33
    YELLOW = 34
    BLUE = 35
    DIGIT_0 = 48
    DIGIT_1 = 49
    DIGIT_2 = 50
    DIGIT_3 = 51
    DIGIT_4 = 52
    DIGIT_5 = 53
    DIGIT_6 = 54
    DIGIT_7 = 55
    DIGIT_8 = 56
    DIGIT_9 = 57
    PLAY = 64
    PAUSE = 65
    STOP = 66
    RECORD = 67
    FAST_FORWARD = 69
    REWIND = 71
    BOX_OFFICE = 240
    SKY = 241


# Names used by pyskyqremote, sky-remote and the old HA integration, so existing
# automations keep working.
KEY_ALIASES: dict[str, Key] = {
    "power": Key.POWER,
    "select": Key.SELECT,
    "backup": Key.BACKUP,
    "dismiss": Key.BACKUP,
    "back": Key.BACKUP,
    "channelup": Key.CHANNEL_UP,
    "channeldown": Key.CHANNEL_DOWN,
    "interactive": Key.INTERACTIVE,
    "sidebar": Key.INTERACTIVE,
    "help": Key.HELP,
    "services": Key.SERVICES,
    "search": Key.SERVICES,
    "tvguide": Key.TV_GUIDE,
    "home": Key.TV_GUIDE,
    "redbutton": Key.RED_BUTTON,
    "i": Key.I,
    "text": Key.TEXT,
    "up": Key.UP,
    "down": Key.DOWN,
    "left": Key.LEFT,
    "right": Key.RIGHT,
    "red": Key.RED,
    "green": Key.GREEN,
    "yellow": Key.YELLOW,
    "blue": Key.BLUE,
    "play": Key.PLAY,
    "pause": Key.PAUSE,
    "stop": Key.STOP,
    "record": Key.RECORD,
    "fastforward": Key.FAST_FORWARD,
    "rewind": Key.REWIND,
    "boxoffice": Key.BOX_OFFICE,
    "sky": Key.SKY,
    **{str(d): Key(Key.DIGIT_0 + d) for d in range(10)},
}


class DeviceType(StrEnum):
    GATEWAY = "GATEWAYSTB"
    MINI = "MULTIROOMSTB"
    IP_SET_TOP_BOX = "IPSETTOPBOX"
    TV = "TV"


UNSUPPORTED_DEVICE_TYPES = frozenset({DeviceType.IP_SET_TOP_BOX, DeviceType.TV})


class PowerState(StrEnum):
    OFF = "off"  # unreachable
    STANDBY = "standby"
    ON = "on"


class TransportState(StrEnum):
    PLAYING = "PLAYING"
    PAUSED = "PAUSED_PLAYBACK"
    STOPPED = "STOPPED"
    TRANSITIONING = "TRANSITIONING"
    NO_MEDIA = "NO_MEDIA_PRESENT"
    UNKNOWN = "UNKNOWN"


class RecordingStatus(StrEnum):
    RECORDED = "RECORDED"
    RECORDING = "RECORDING"
    PART_RECORDED = "PART REC"
    SCHEDULED = "SCHEDULED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"
