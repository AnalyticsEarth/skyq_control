from __future__ import annotations

import json
from datetime import UTC, date, datetime

from aioskyq import (
    AppsStatus,
    Channel,
    DeviceInfo,
    EpgEvent,
    MediaInfo,
    PositionInfo,
    Recording,
    RecordingList,
    RecordingStatus,
    Storage,
    SystemStatus,
    SystemTime,
    TransportInfo,
    TransportState,
)

from .fakebox import load


def test_device_info_gateway_and_mini() -> None:
    gw = DeviceInfo.from_api(
        "h", load("gateway/rest/system_deviceinformation.json"), load("gateway/rest/system_information.json")
    )
    assert gw.is_gateway and gw.device_type == "GATEWAYSTB"
    assert gw.gateway_ip_address is None  # gateways report "not MR"
    assert gw.bouquet == 4101 and gw.subbouquet == 1
    assert gw.uhd_capable and gw.hdr_capable and gw.pip_capable
    assert gw.hardware_model == "ES240" and gw.ca_type == "VIDEOGUARD"
    assert gw.boot_time is not None and gw.boot_time < datetime.now(UTC)
    assert gw.unique_id  # redacted in fixtures, but still present

    mini = DeviceInfo.from_api(
        "h", load("mini/rest/system_deviceinformation.json"), load("mini/rest/system_information.json")
    )
    assert not mini.is_gateway and mini.device_type == "MULTIROOMSTB"
    assert mini.gateway_ip_address == "192.0.2.10"


def test_system_time() -> None:
    time = SystemTime.from_api(load("gateway/rest/system_time.json"))
    assert time.utc is not None and time.utc.year == 2026
    assert time.present_offset == 0 and time.future_offset == 3600
    assert time.timezone.utcoffset(None).total_seconds() == 0


def test_system_status() -> None:
    status = SystemStatus.from_api(load("gateway/ws/system_status.json"))
    assert "ULTRA+" in status.entitlements and "SKY+" in status.entitlements
    assert status.is_active
    assert status.epg_last_date == date(2026, 11, 1)
    assert status.section_state("pvr") == "available"
    assert status.section_state("missing") is None
    assert status.smartcard["paired"] is True
    assert status.software_update_reason == "IDLE"


def test_channels() -> None:
    channels = [Channel.from_api(s) for s in load("rest/services_bouquet.json")["services"]]
    by_number = {c.number: c for c in channels}
    assert by_number["101"].sid == 3008 and by_number["101"].format == "hd"
    assert by_number["401"].is_uhd
    assert any(c.is_radio for c in channels)
    assert by_number["101"].logo_url.endswith("skychb3008.png")


def test_apps_status_visible() -> None:
    status = AppsStatus.from_api(
        {"apps": [{"appId": "a", "status": "RUNNING"}, {"appId": "YouTube", "status": "VISIBLE"}]}
    )
    assert status.visible_app == "YouTube"
    # Captured while the gateway was showing HBO Max (GetMediaInfo's URI is empty then).
    assert AppsStatus.from_api(load("gateway/ws/apps_status.json")).visible_app == "YouTube"


def test_recordings() -> None:
    listing = RecordingList.from_api(load("rest/pvr_list.json"))
    assert listing.total == 7
    statuses = {r.status for r in listing.recordings}
    assert statuses == {RecordingStatus.RECORDED, RecordingStatus.PART_RECORDED, RecordingStatus.FAILED}
    part = next(r for r in listing.recordings if r.pvrid == "P2902aeb9")
    assert part.resume_position == 2226 and part.eid == "Efd2-3f3"
    assert not part.keep and not part.locked and not part.series_linked  # flags are omitted when false
    assert part.start is not None and part.end is not None and part.end > part.start
    assert part.image_url is not None


def test_recording_states_and_images() -> None:
    scheduled = Recording.from_api(
        {"pvrid": "P1", "status": "SCHEDULED", "st": 1_800_000_000, "schd": 1800, "osid": 3008}
    )
    assert scheduled.start == datetime.fromtimestamp(1_800_000_000, UTC)
    assert scheduled.duration == 1800
    assert scheduled.image_url and "skychb3008" in scheduled.image_url
    recording = Recording.from_api({"pvrid": "P2", "status": "RECORDING", "st": 100, "ast": 160, "schd": 600})
    assert recording.end == datetime.fromtimestamp(700, UTC)  # ends at the scheduled time
    odd = Recording.from_api(
        {"pvrid": "P3", "status": "SOMETHING NEW", "del": 5, "et": "UHD-HDR", "imageuri_16x9": "http://x/i.jpg"}
    )
    assert odd.status is RecordingStatus.UNKNOWN and odd.status_raw == "SOMETHING NEW"
    assert odd.is_deleted and odd.is_uhd and odd.image_url == "http://x/i.jpg"


def test_changes_feed() -> None:
    changes = RecordingList.from_api(load("gateway/rest/pvr_changes_current.json"))
    assert changes.version == 19 and changes.recordings[0].change_type == "update"
    assert changes.recordings[0].is_uhd


def test_storage() -> None:
    storage = Storage.from_api(load("gateway/rest/pvr_storage.json"))
    assert storage.free_mb == storage.quota_mb - storage.used_mb
    assert 0 < storage.used_percent < 100
    assert Storage(quota_mb=0, used_mb=0).used_percent == 0.0


def test_transport_and_media() -> None:
    transport = TransportInfo.from_api(load("mini/soap/SkyPlay_GetTransportInfo.json"))
    assert transport.state is TransportState.PLAYING and transport.is_playing and transport.speed == 1.0
    assert TransportInfo.from_api({"CurrentTransportState": "WEIRD"}).state is TransportState.UNKNOWN

    live = MediaInfo.from_api({"CurrentURI": "xsi://BC0"})
    assert live.is_live and live.sid == 3008 and not live.is_recording
    rec = MediaInfo.from_api({"CurrentURI": "file://pvr/2902aeb9"})
    assert rec.is_recording and rec.pvrid == "P2902aeb9"
    assert MediaInfo.from_api({"CurrentURI": None}).uri is None
    assert MediaInfo.from_api({"CurrentURI": "xsi://zz"}).sid is None


def test_position() -> None:
    position = PositionInfo.from_api({"RelTime": "0:01:05", "TrackDuration": "01:00:00"})
    assert position.position == 65 and position.duration == 3600
    assert PositionInfo.from_api({"RelTime": "NOT_IMPLEMENTED"}).position is None


def test_epg_event() -> None:
    raw = load("epg/hawk_schedule.json")["schedule"][0]["events"][3]
    event = EpgEvent.from_api(3008, raw)
    assert event.eid.startswith("Ebc0-")  # E{sid hex}-…, the same form as PVR oeid
    assert (event.end - event.start).total_seconds() == event.duration
    assert event.is_on_at(event.start) and not event.is_on_at(event.end)
    assert event.image_url is None or event.image_url.startswith("https://images.metadata.sky.com/")
    json.dumps(event.raw)
