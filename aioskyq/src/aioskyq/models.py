"""Typed views of Sky Q payloads.

Every model keeps the untouched payload in ``raw`` so callers can reach fields this
library does not (yet) name. Field meanings come from Sky's PVR Swagger spec and the
2026-10-25 probe of real boxes; short Sky names are noted next to each attribute.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta, timezone
from typing import Any

from .const import (
    CHANNEL_LOGO_URL,
    PROGRAMME_IMAGE_URL,
    DeviceType,
    RecordingStatus,
    TransportState,
)

Raw = dict[str, Any]


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _ts(value: Any) -> datetime | None:
    seconds = _int(value)
    return datetime.fromtimestamp(seconds, tz=UTC) if seconds else None


def _hms(value: Any) -> int | None:
    """Parse UPnP H:MM:SS into seconds (None for NOT_IMPLEMENTED and friends)."""
    if not isinstance(value, str) or value.count(":") != 2:
        return None
    try:
        hours, minutes, seconds = (int(float(part)) for part in value.split(":"))
    except ValueError:
        return None
    return hours * 3600 + minutes * 60 + seconds


def programme_image_url(programme_uuid: str, width: int = 640) -> str:
    """16:9 artwork for a programme (widths 640 and 1024 are known to work)."""
    return PROGRAMME_IMAGE_URL.format(uuid=programme_uuid, width=width)


def channel_logo_url(sid: int | str) -> str:
    return CHANNEL_LOGO_URL.format(sid=sid)


# --------------------------------------------------------------------------- device


@dataclass(frozen=True, slots=True, kw_only=True)
class DeviceInfo:
    """Merged /as/system/deviceinformation and /as/system/information."""

    host: str
    as_version: str | None
    model_number: str | None
    version_number: str | None
    hardware_name: str | None
    hardware_model: str | None
    manufacturer: str | None
    device_type: str | None
    serial_number: str | None
    mac_address: str | None
    ip_address: str | None
    country_code: str | None
    bouquet: int | None
    subbouquet: int | None
    gateway: bool
    gateway_ip_address: str | None
    active_standby: bool
    wake_reason: str | None
    system_uptime: int | None
    hdr_capable: bool
    uhd_capable: bool
    pip_capable: bool
    streaming_capable: bool
    ca_type: str | None
    drm_activated: bool | None
    mesh_enabled: bool | None
    num_dtt_tuners: int | None
    purchase_balance: str | None
    purchase_limit: str | None
    raw: Raw = field(repr=False)

    @classmethod
    def from_api(cls, host: str, device_information: Raw, system_information: Raw) -> DeviceInfo:
        merged = {**system_information, **device_information}
        gateway_ip = merged.get("gatewayIPAddress")
        return cls(
            host=host,
            as_version=merged.get("ASVersion"),
            model_number=merged.get("modelNumber"),
            version_number=merged.get("versionNumber"),
            hardware_name=merged.get("hardwareName"),
            hardware_model=merged.get("hardwareModel"),
            manufacturer=merged.get("manufacturer"),
            device_type=merged.get("deviceType"),
            serial_number=merged.get("serialNumber"),
            mac_address=merged.get("MACAddress"),
            ip_address=merged.get("IPAddress"),
            country_code=merged.get("countryCode"),
            bouquet=_int(merged.get("bouquet")),
            subbouquet=_int(merged.get("subbouquet")),
            gateway=bool(merged.get("gateway")),
            # Gateways report the literal string "not MR".
            gateway_ip_address=gateway_ip if gateway_ip and gateway_ip[0].isdigit() else None,
            active_standby=bool(merged.get("activeStandby")),
            wake_reason=merged.get("wakeReason"),
            system_uptime=_int(merged.get("systemUptime")),
            hdr_capable=bool(merged.get("hdrCapable")),
            uhd_capable=bool(merged.get("uhdCapable")),
            pip_capable=bool(merged.get("pipCapable")),
            streaming_capable=bool(merged.get("streamingCapable")),
            ca_type=merged.get("CAType"),
            drm_activated=merged.get("DRMActivationStatus"),
            mesh_enabled=merged.get("meshEnabled"),
            num_dtt_tuners=_int(merged.get("numDTTTuners")),
            purchase_balance=merged.get("purchasebalance"),
            purchase_limit=merged.get("purchaselimit"),
            raw=merged,
        )

    @property
    def is_gateway(self) -> bool:
        return self.gateway or self.device_type == DeviceType.GATEWAY

    @property
    def unique_id(self) -> str | None:
        """Stable identifier: serial number, else MAC address."""
        return self.serial_number or self.mac_address

    @property
    def boot_time(self) -> datetime | None:
        if self.system_uptime is None:
            return None
        return datetime.now(UTC) - timedelta(seconds=self.system_uptime)


@dataclass(frozen=True, slots=True, kw_only=True)
class SystemTime:
    """/as/system/time. Offsets are seconds from UTC."""

    utc: datetime | None
    present_offset: int
    future_offset: int | None
    future_transition: datetime | None
    raw: Raw = field(repr=False)

    @classmethod
    def from_api(cls, data: Raw) -> SystemTime:
        return cls(
            utc=_ts(data.get("utc")),
            present_offset=_int(data.get("presentLocalTimeOffset")) or 0,
            future_offset=_int(data.get("futureLocalTimeOffset")),
            future_transition=_ts(data.get("futureTransitionUtc")),
            raw=data,
        )

    @property
    def timezone(self) -> timezone:
        return timezone(timedelta(seconds=self.present_offset))


@dataclass(frozen=True, slots=True, kw_only=True)
class SystemStatus:
    """A message from the /as/system/status websocket (full snapshot on connect, then updates)."""

    entitlements: tuple[str, ...]
    system_state: str | None
    software_update_state: str | None
    software_update_reason: str | None
    epg_last_date: date | None
    smartcard: Raw
    hdmi: Raw | None
    gateway_services: Raw
    raw: Raw = field(repr=False)

    @classmethod
    def from_api(cls, data: Raw) -> SystemStatus:
        last = (data.get("schedule") or {}).get("lastdate")
        try:
            epg_last = datetime.strptime(last, "%Y%m%d").date() if last else None
        except ValueError:
            epg_last = None
        swupdate = data.get("swupdate") or {}
        return cls(
            entitlements=tuple(data.get("entitlements") or ()),
            system_state=(data.get("systemstate") or {}).get("state"),
            software_update_state=swupdate.get("state"),
            software_update_reason=swupdate.get("reason"),
            epg_last_date=epg_last,
            smartcard=data.get("smartcard") or {},
            hdmi=data.get("hdmi"),
            gateway_services=data.get("gatewayservices") or {},
            raw=data,
        )

    def section_state(self, name: str) -> str | None:
        """`state` of a section such as drmstatus, network, pvr, servicelist, nssplayback."""
        section = self.raw.get(name)
        return section.get("state") if isinstance(section, dict) else None

    @property
    def is_active(self) -> bool:
        return self.system_state == "active"


# --------------------------------------------------------------------------- channels & apps


@dataclass(frozen=True, slots=True, kw_only=True)
class Channel:
    sid: int
    number: str  # c
    name: str  # t
    format: str | None  # sf: sd / hd / uhd / au (radio)
    genre: int | None  # sg
    extended_genre: int | None  # xsg
    service_type: str | None
    dvb_triplet: str | None
    has_schedule: bool
    adult: bool
    raw: Raw = field(repr=False)

    @classmethod
    def from_api(cls, data: Raw) -> Channel:
        return cls(
            sid=int(data["sid"]),
            number=str(data.get("c", "")),
            name=str(data.get("t", "")),
            format=(data.get("sf") or "").lower() or None,
            genre=_int(data.get("sg")),
            extended_genre=_int(data.get("xsg")),
            service_type=data.get("servicetype"),
            dvb_triplet=data.get("dvbtriplet"),
            has_schedule=data.get("schedule") in (True, "true"),
            adult=bool(data.get("adult")),
            raw=data,
        )

    @property
    def is_radio(self) -> bool:
        return self.format == "au"

    @property
    def is_uhd(self) -> bool:
        return self.format == "uhd"

    @property
    def logo_url(self) -> str:
        return channel_logo_url(self.sid)


@dataclass(frozen=True, slots=True, kw_only=True)
class StreamingProfile:
    name: str | None
    uri: str | None
    caption_uri: str | None


@dataclass(frozen=True, slots=True, kw_only=True)
class ChannelDetails:
    """/as/services/details/{sid}. Stream URIs on port 4730 are DRM-encrypted."""

    sid: int
    dvb_triplet: str | None
    is_broadcasting: bool
    description: str | None
    streaming_profiles: tuple[StreamingProfile, ...]
    raw: Raw = field(repr=False)

    @classmethod
    def from_api(cls, data: Raw) -> ChannelDetails:
        details = data.get("details") or {}
        return cls(
            sid=int(data["sid"]),
            dvb_triplet=details.get("dvbtriplet"),
            is_broadcasting=bool(details.get("isbroadcasting")),
            description=details.get("upgradeMessage"),
            streaming_profiles=tuple(
                StreamingProfile(name=p.get("name"), uri=p.get("uri"), caption_uri=p.get("suri"))
                for p in data.get("streamingprofiles") or ()
            ),
            raw=data,
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class Genre:
    genre: int  # sg
    extended_genre: int  # xsg
    title: str

    @classmethod
    def from_api(cls, data: Raw) -> Genre:
        return cls(genre=int(data["sg"]), extended_genre=int(data["xsg"]), title=str(data["t"]))


@dataclass(frozen=True, slots=True, kw_only=True)
class Favourite:
    position: int  # lcn
    sid: int

    @classmethod
    def from_api(cls, data: Raw) -> Favourite:
        return cls(position=int(data["lcn"]), sid=int(data["sid"]))


@dataclass(frozen=True, slots=True, kw_only=True)
class App:
    app_id: str
    title: str
    version: str | None
    blocked: bool
    pin_locked: bool
    raw: Raw = field(repr=False)

    @classmethod
    def from_api(cls, data: Raw) -> App:
        return cls(
            app_id=str(data["appId"]),
            title=str(data.get("title") or data["appId"]),
            version=data.get("version"),
            blocked=bool(data.get("blocked")),
            pin_locked=bool(data.get("pinLocked")),
            raw=data,
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class AppsStatus:
    """A message from /as/apps/status. `statuses` maps appId -> RUNNING / VISIBLE / …"""

    statuses: dict[str, str]
    raw: Raw = field(repr=False)

    @classmethod
    def from_api(cls, data: Raw) -> AppsStatus:
        return cls(
            statuses={str(a["appId"]): str(a.get("status")) for a in data.get("apps") or () if "appId" in a},
            raw=data,
        )

    @property
    def visible_app(self) -> str | None:
        return next((app for app, status in self.statuses.items() if status == "VISIBLE"), None)


# --------------------------------------------------------------------------- PVR


@dataclass(frozen=True, slots=True, kw_only=True)
class Recording:
    pvrid: str
    status: RecordingStatus
    status_raw: str | None
    title: str | None  # t
    synopsis: str | None  # sy
    channel_name: str | None  # cn
    channel_number: str | None  # c
    sid: int | None  # osid
    eid: str | None  # oeid, matches EPG event ids
    scheduled_start: datetime | None  # st
    actual_start: datetime | None  # ast
    scheduled_duration: int | None  # schd, seconds
    final_duration: int | None  # finald, seconds
    resume_position: int | None  # lp, seconds
    last_played: datetime | None  # lpt
    deleted_at: datetime | None  # del
    expires: datetime | None  # exp
    failure_reason: str | None  # fr
    source: str | None  # src: LIVE / VOD / EST3
    keep: bool
    locked: bool
    series_linked: bool  # link
    hd: bool
    event_type: str | None  # et, e.g. "UHD-HDR"
    season: int | None
    episode: int | None
    programme_uuid: str | None
    series_uuid: str | None
    season_uuid: str | None
    rating: str | None  # r
    subtitles: bool  # s
    audio_description: bool  # ad
    size_kb: int | None  # finalsz, else predictedsz
    change_type: str | None  # type, only in /as/pvr/changes ("update", …)
    raw: Raw = field(repr=False)

    @classmethod
    def from_api(cls, data: Raw) -> Recording:
        status_raw = data.get("status")
        try:
            status = RecordingStatus(str(status_raw))
        except ValueError:
            status = RecordingStatus.UNKNOWN
        return cls(
            pvrid=str(data["pvrid"]),
            status=status,
            status_raw=status_raw,
            title=data.get("t"),
            synopsis=data.get("sy"),
            channel_name=data.get("cn"),
            channel_number=data.get("c"),
            sid=_int(data.get("osid")),
            eid=data.get("oeid"),
            scheduled_start=_ts(data.get("st")),
            actual_start=_ts(data.get("ast")),
            scheduled_duration=_int(data.get("schd")),
            final_duration=_int(data.get("finald")),
            resume_position=_int(data.get("lp")),
            last_played=_ts(data.get("lpt")),
            deleted_at=_ts(data.get("del")),
            expires=_ts(data.get("exp")),
            failure_reason=data.get("fr"),
            source=data.get("src"),
            keep=bool(data.get("keep")),
            locked=bool(data.get("lock")),
            series_linked=bool(data.get("link")),
            hd=bool(data.get("hd")),
            event_type=data.get("et"),
            season=_int(data.get("seasonnumber")),
            episode=_int(data.get("episodenumber")),
            programme_uuid=data.get("programmeuuid"),
            series_uuid=data.get("seriesuuid"),
            season_uuid=data.get("seasonuuid"),
            rating=data.get("r"),
            subtitles=bool(data.get("s")),
            audio_description=bool(data.get("ad")),
            size_kb=_int(data.get("finalsz") or data.get("predictedsz")),
            change_type=data.get("type"),
            raw=data,
        )

    @property
    def start(self) -> datetime | None:
        if self.status is RecordingStatus.SCHEDULED:
            return self.scheduled_start
        return self.actual_start or self.scheduled_start

    @property
    def duration(self) -> int | None:
        if self.status in (RecordingStatus.SCHEDULED, RecordingStatus.RECORDING):
            return self.scheduled_duration
        return self.final_duration or self.scheduled_duration

    @property
    def end(self) -> datetime | None:
        if self.status is RecordingStatus.RECORDING and self.scheduled_start and self.scheduled_duration:
            # A recording that started late still ends at its scheduled time.
            return self.scheduled_start + timedelta(seconds=self.scheduled_duration)
        if self.start and self.duration:
            return self.start + timedelta(seconds=self.duration)
        return None

    @property
    def is_deleted(self) -> bool:
        """Soft-deleted items sit in the trash until `deleted_at` + retention."""
        return self.deleted_at is not None

    @property
    def is_uhd(self) -> bool:
        return bool(self.event_type and "UHD" in self.event_type) or bool(self.raw.get("uhd"))

    @property
    def image_url(self) -> str | None:
        if url := self.raw.get("imageuri_16x9"):
            return str(url)
        if self.programme_uuid:
            return programme_image_url(self.programme_uuid)
        if self.sid:
            return channel_logo_url(self.sid)
        return None


@dataclass(frozen=True, slots=True, kw_only=True)
class RecordingList:
    """/as/pvr (full list) or /as/pvr/changes/{documentId}/{version} (diff)."""

    document_id: str | None
    version: int | None
    total: int | None
    recordings: tuple[Recording, ...]
    raw: Raw = field(repr=False)

    @classmethod
    def from_api(cls, data: Raw) -> RecordingList:
        return cls(
            document_id=data.get("documentId"),
            version=_int(data.get("version")),
            total=_int(data.get("totalPvrItems")),
            recordings=tuple(Recording.from_api(item) for item in data.get("pvrItems") or ()),
            raw=data,
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class PvrStatus:
    """A message from /as/pvr/status: the PVR document changed; fetch /as/pvr/changes."""

    document_id: str | None
    min_version: int | None
    max_version: int | None
    raw: Raw = field(repr=False)

    @classmethod
    def from_api(cls, data: Raw) -> PvrStatus:
        return cls(
            document_id=data.get("documentId"),
            min_version=_int(data.get("min")),
            max_version=_int(data.get("max")),
            raw=data,
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class Storage:
    """/as/pvr/storage. Units are megabytes (a 2 TB ES240 reports ~1.6 M)."""

    quota_mb: int
    used_mb: int

    @classmethod
    def from_api(cls, data: Raw) -> Storage:
        return cls(quota_mb=int(data["userQuotaMax"]), used_mb=int(data["userQuotaUsed"]))

    @property
    def free_mb(self) -> int:
        return max(self.quota_mb - self.used_mb, 0)

    @property
    def used_percent(self) -> float:
        return round(100 * self.used_mb / self.quota_mb, 1) if self.quota_mb else 0.0


@dataclass(frozen=True, slots=True, kw_only=True)
class PinStatus:
    tries_left: int | None
    seconds_to_unblock: int | None

    @classmethod
    def from_api(cls, data: Raw) -> PinStatus:
        return cls(tries_left=_int(data.get("triesLeft")), seconds_to_unblock=_int(data.get("timeToUnblock")))


# --------------------------------------------------------------------------- playback (SOAP)


@dataclass(frozen=True, slots=True, kw_only=True)
class TransportInfo:
    state: TransportState
    state_raw: str | None
    status: str | None
    speed: float | None  # trick-play speed: 1 normal, negative rewind

    @classmethod
    def from_api(cls, data: Raw) -> TransportInfo:
        raw_state = data.get("CurrentTransportState")
        try:
            state = TransportState(str(raw_state))
        except ValueError:
            state = TransportState.UNKNOWN
        try:
            speed = float(data["CurrentSpeed"])
        except (KeyError, TypeError, ValueError):
            speed = None
        return cls(state=state, state_raw=raw_state, status=data.get("CurrentTransportStatus"), speed=speed)

    @property
    def is_playing(self) -> bool:
        return self.state in (TransportState.PLAYING, TransportState.TRANSITIONING)


@dataclass(frozen=True, slots=True, kw_only=True)
class MediaInfo:
    """GetMediaInfo: `xsi://{sid hex}` for live TV, `file://pvr/{id}` for a recording."""

    uri: str | None
    sid: int | None
    pvrid: str | None

    @classmethod
    def from_api(cls, data: Raw) -> MediaInfo:
        uri = data.get("CurrentURI") or None
        sid = pvrid = None
        if uri and uri.startswith("xsi://"):
            sid = _int_hex(uri[6:])
        elif uri and uri.startswith("file://pvr/"):
            pvrid = f"P{uri[11:]}"
        return cls(uri=uri, sid=sid, pvrid=pvrid)

    @property
    def is_live(self) -> bool:
        return self.sid is not None

    @property
    def is_recording(self) -> bool:
        return self.pvrid is not None


def _int_hex(value: str) -> int | None:
    try:
        return int(value, 16)
    except ValueError:
        return None


@dataclass(frozen=True, slots=True, kw_only=True)
class PositionInfo:
    """GetPositionInfo. Live TV reports 00:00:00; recordings are still to be confirmed."""

    position: int | None
    duration: int | None
    uri: str | None

    @classmethod
    def from_api(cls, data: Raw) -> PositionInfo:
        return cls(
            position=_hms(data.get("RelTime")), duration=_hms(data.get("TrackDuration")), uri=data.get("TrackURI")
        )


# --------------------------------------------------------------------------- EPG


@dataclass(frozen=True, slots=True, kw_only=True)
class EpgEvent:
    sid: int
    eid: str
    title: str
    synopsis: str | None
    start: datetime
    duration: int
    season: int | None
    episode: int | None
    programme_uuid: str | None
    series_uuid: str | None
    rating: str | None
    genre: int | None  # eg
    sub_genre: int | None  # esg
    subtitles: bool  # s
    audio_description: bool  # ad
    hd: bool
    new: bool
    can_book: bool  # canb
    can_series_link: bool  # canl
    restartable: bool
    pay_per_view: bool  # ippv / oppv
    raw: Raw = field(repr=False)

    @classmethod
    def from_api(cls, sid: int, data: Raw) -> EpgEvent:
        return cls(
            sid=sid,
            eid=str(data["eid"]),
            title=str(data.get("t", "")),
            synopsis=data.get("sy"),
            start=datetime.fromtimestamp(int(data["st"]), tz=UTC),
            duration=int(data.get("d", 0)),
            season=_int(data.get("seasonnumber")) or None,
            episode=_int(data.get("episodenumber")) or None,
            programme_uuid=data.get("programmeuuid"),
            series_uuid=data.get("seriesuuid"),
            rating=data.get("r"),
            genre=_int(data.get("eg")),
            sub_genre=_int(data.get("esg")),
            subtitles=bool(data.get("s")),
            audio_description=bool(data.get("ad")),
            hd=bool(data.get("hd")),
            new=bool(data.get("new")),
            can_book=bool(data.get("canb")),
            can_series_link=bool(data.get("canl")),
            restartable=bool(data.get("restartable")),
            pay_per_view=bool(data.get("ippv") or data.get("oppv")),
            raw=data,
        )

    @property
    def end(self) -> datetime:
        return self.start + timedelta(seconds=self.duration)

    @property
    def image_url(self) -> str | None:
        return programme_image_url(self.programme_uuid) if self.programme_uuid else None

    def is_on_at(self, when: datetime) -> bool:
        return self.start <= when < self.end


@dataclass(frozen=True, slots=True, kw_only=True)
class NowPlaying:
    """Combined view of what a box is showing right now."""

    transport: TransportInfo
    media: MediaInfo
    channel: Channel | None = None
    programme: EpgEvent | None = None
    recording: Recording | None = None
    position: PositionInfo | None = None
