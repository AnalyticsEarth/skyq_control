# aioskyq

Async Python client for the local network API of **Sky Q** set-top boxes (gateway and Mini). It is built for the Home Assistant `skyq_control` integration, but has no Home Assistant dependency.

Everything here was checked against real boxes (firmware Q370, October 2026). The background is in [`docs/PLAN.md`](../docs/PLAN.md).

```python
import asyncio
from aioskyq import SkyQBox, discover


async def main():
    boxes = await discover()  # SSDP; gateways first
    async with SkyQBox(boxes[0].host) as box:
        info = await box.get_device_info()
        print(info.hardware_model, info.as_version)

        now = await box.get_now_playing()  # channel + current programme
        print(now.channel.name if now.channel else now.media.uri, now.programme.title if now.programme else "")

        await box.change_channel(102)  # typed as digits
        await box.launch_app("YouTube")

        for rec in (await box.get_recordings()).recordings[:5]:
            print(rec.status, rec.title, rec.start)

        box.subscribe_system_status(lambda s: print("state:", s.system_state))
        await asyncio.sleep(60)


asyncio.run(main())
```

## What it covers

| Area | Methods | Transport |
|---|---|---|
| Device | `get_device_info`, `get_power_state`, `get_system_time`, `ping` | REST 9006 |
| Channels & apps | `get_channels`, `get_channel(_by_number)`, `get_channel_details`, `get_genres`, `get_favourites`, `get_apps`, `launch_app` | REST |
| Recordings | `get_recordings` (paged), `get_recording`, `get_recording_changes`, `get_storage`, `get_pin_status` | REST |
| Recording actions | `book_recording(eid, series=)`, `cancel_booking`, `stop_recording`, `delete/undelete/erase_recording`, `erase_all_recordings(confirm=True)`, `set_keep`, `set_lock`, `set_series_link`, `set_resume_position` (forward only) | REST |
| Playback state | `get_transport_info`, `get_media_info`, `get_position_info`, `get_now_playing` | SOAP 49153 (read only) |
| Control | `press(*keys)`, `change_channel`, `tune(sid)`, `turn_on/off`, `play/pause/stop/fast_forward/rewind/record` | Remote keys, TCP 49160 |
| Guide | `box.epg.get_day`, `get_events`, `now_and_next`, `find_event`; `get_schedule` | Sky's hawk / atlantis hosts |
| Push | `subscribe_system_status`, `subscribe_apps`, `subscribe_pvr`, `subscribe_channel_list`, `subscribe_connection` | Websockets on 9006 |

## Sky Q quirks this library handles

- **SOAP control is locked.** `Pause` and `SetAVTransportURI` return UPnP 705 `TRANSPORT IS LOCKED`, so playback and tuning use remote keys and SOAP is read-only.
- **Gateways list SkyPlay twice.** The copy on the SkyServe device rejects every action, so the client uses the copy that answers.
- **The resume position is forward-only.** Moving it backwards returns 500 and then hangs the request, so `set_resume_position` refuses earlier positions.
- **Bookings return an empty body.** `book_recording` finds the new item by `oeid`. `cancel_booking` (a delete) takes effect a few seconds later.
- **Channel changes are not pushed.** Poll `get_media_info` (the integration does every few seconds). App changes, standby/wake and PVR changes are pushed.
- **Any `/as/…/status` path accepts a websocket**, but only system, apps, pvr and services send data. The gateway can stall a socket silently, so streams reconnect every 10 minutes and after errors. Every connect delivers a full snapshot.
- **The box has no guide.** The EPG comes from `awk.epgsky.com` (20 SIDs per request, about 8 days ahead) and, for UHD channels, `atlantis.epgsky.com` with `X-SkyOTT-*` headers.

## Development

```bash
cd aioskyq
uv sync
uv run pytest            # tests run against a fake box built from sanitized real fixtures
uv run ruff check . && uv run mypy
SKYQ_HOST=192.168.1.50 uv run pytest -m live   # read-only checks against a real box
```

`tests/build_fixtures.py` rebuilds `tests/fixtures/` from `tools/skyq_probe.py` output.
