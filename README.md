# Sky Q Control for Home Assistant

A local-push Home Assistant integration that aims to expose **every control and every piece of metadata** a Sky Q box offers on the local network. It supports gateways and Minis. Sky Glass and Sky Stream use a different platform and are not supported.

> **Status: v0.6.0.** Everything in the plan is in: a media player with a guide and recordings browser, a remote, a button for every remote key, channel and app pickers, sensors, binary sensors, artwork images, recordings calendars, events and device triggers, actions for automations covering the guide and recordings, and a ready-made dashboard. See [docs/PLAN.md](docs/PLAN.md).

## What you get per box

| Entity | What it does |
|---|---|
| **Media player** (`media_player.sky_q_…`) | On/off, play/pause/stop, channel up/down (next/previous), and a source list of every channel on the box. Shows the channel, the current programme with artwork and a live progress bar, the next programme, a recording's title and position, or the app on screen. **Browse media** opens the TV guide: favourites, recordings (listed only; Sky Q blocks remote playback), channels by genre (each showing what's on now) and apps. Open a channel to see its next 24 hours; play a channel to tune to it. |
| **Remote** (`remote.sky_q_…`) | Every remote key through `remote.send_command`, with repeats and delays. Works with HA's remote card. |
| **Buttons** | One per remote key, all enabled. |
| **Selects** | **Channel** (every channel) and **App** (installed apps such as YouTube and Netflix). Picking an option tunes the channel or opens the app. |
| **Sensors** | What's on (channel, programme, when it ends, next programme, app, playback state). On the gateway: disk usage, used, free and size, and counts of recordings, scheduled, failed and deleted recordings, what's recording now and the next recording. Diagnostics: software version, firmware build, last boot, wake reason, bouquet, guide available until, software update, entitlements, smartcard, PIN tries left, and on Minis the gateway's address. Household ID and viewing-card number are disabled by default. |
| **Binary sensors** | Power, recording (gateway), watching a recording, gateway connection (Minis), network, recorder, DRM and guide-data problems, software update, smartcard active, and UHD, HDR, picture-in-picture and streaming capability. |
| **Images** | Programme artwork and the channel logo. |
| **Events** | **Playback** (channel changed, app opened or closed) and, on the gateway, **Recordings** (started, finished, failed), with details as attributes. |
| **Calendars** (gateway) | **Scheduled recordings** (including anything recording now) and **Recordings** already made, each with the channel, synopsis and recording ID. |

**Actions for automations** (target a Sky Q media player):
- `skyq_control.send_keys`: keys such as `backup, 1, 0, 1` or `tv_guide`, with an optional delay between them.
- `skyq_control.change_channel`: a channel number such as `101`, or `sid:3008`.
- `skyq_control.launch_app`: an app name or ID such as `YouTube`.
- `skyq_control.get_guide` (returns data): programmes on the channels you name (numbers, names or `sid:3008`), or the channel the box is showing, from a start time for up to 48 hours. Add `search` to find a title on every channel. Each programme has an `event_id`.
- `skyq_control.record`: books a recording from an `event_id`, with `series: true` for a series link. `skyq_control.record_now` records what's on.
- `skyq_control.get_recordings` (returns data): the household's recordings, filtered by status (recorded, scheduled, recording, failed, deleted) or title.
- `skyq_control.delete_recording` (to the trash, or `permanent: true`; also cancels a booking), `restore_recording`, `stop_recording`, and `update_recording` (keep, locked, series link, resume position).
- `skyq_control.get_channels` and `skyq_control.get_apps` (return data).

Recording actions work from any box; a Mini passes them on to the gateway. Erasing every recording at once and pay-per-view booking are deliberately not offered.

```yaml
action: skyq_control.get_guide
target:
  entity_id: media_player.sky_q_gateway
data:
  search: Match of the Day
  hours: 48
response_variable: guide
```

`media_player.play_media` also accepts `channel`, `app` and `keys` as media types.

## Automations

Each box's device offers these triggers: **channel changed**, **app opened or closed**, and on the gateway **recording started**, **finished** and **failed**. Turned on/off comes from the media player, and disk-usage thresholds from the Disk usage sensor. The same events are fired on the bus as `skyq_control_event`, with `device_id`, `type` and details (channel, app, recording title and ID, failure reason):

```yaml
triggers:
  - trigger: event
    event_type: skyq_control_event
    event_data:
      type: recording_failed
actions:
  - action: notify.notify
    data:
      message: "Sky Q couldn't record {{ trigger.event.data.title }}: {{ trigger.event.data.failure_reason }}"
```

Other ideas: dim the lights when a film channel comes on (`channel_changed`), pause the robot vacuum while an app is open, or record the next episode with `skyq_control.record` and the media player's `next_event_id`.

## Dashboard

[docs/dashboard.yaml](docs/dashboard.yaml) is a ready-made dashboard built from built-in cards: a full remote (D-pad, colour keys, transport, number pad), now playing with artwork and now/next, channel and app pickers, a disk-usage gauge, recording counts and the recordings calendars. Create a new dashboard, open its raw configuration editor and paste the file. It uses the gateway's entities; for a Mini, replace `sky_q_gateway` with the Mini's name.

Under the hood:
- Playback is polled every 5 s while a box is on (30 s in standby), because the box doesn't announce channel changes. App, standby, recording and channel-list changes arrive by push.
- Commands go over the box's remote-control port, because Sky Q blocks the UPnP transport controls.
- The guide comes from Sky's public metadata service.
- Diagnostics redact serials, card numbers and household IDs.

## Install with HACS

1. In Home Assistant, open **HACS → ⋮ → Custom repositories**.
2. Add `https://github.com/AnalyticsEarth/skyq_control` with type **Integration**.
3. Find **Sky Q Control** in HACS, download it, and restart Home Assistant.
4. Your boxes should be discovered automatically (**Settings → Devices & services**). Otherwise, choose **Add integration → Sky Q Control** and pick or type a box's IP address. Add the gateway first so Minis are linked to it.

Give each box a fixed IP address in your router. The client library (`aioskyq`) is bundled inside the integration, so nothing is installed from PyPI.

## Supported devices

Sky Q gateways (the main box: ES130, ES140, ES240, ES340) and Sky Q Minis (EM150), in the UK and Ireland. Developed against an ES240 gateway and three EM150 Minis on Sky Q software Q370. Sky Glass, Sky Stream and Sky+HD aren't Sky Q and aren't supported; Germany and Italy may work but are untested.

## Known limitations

- **No remote playback of recordings.** Sky Q locks its UPnP transport, so recordings can be listed, booked, kept, locked and deleted, but not started from Home Assistant.
- **Channel changes aren't pushed** by the box, so they're picked up by polling (every 5 s by default).
- **Channels are changed by typing their number**, so the box shows its number entry briefly. If digits get missed, raise *Delay between key presses* in the integration's options.
- **The guide comes from Sky's public, unofficial metadata service** (no account needed). If Sky changes it, the guide, now/next and artwork stop until the integration is updated; everything else keeps working.
- **No HDMI/HDCP details:** current Sky Q firmware doesn't report them.
- **Minis in standby** suspend their link to the gateway; the *Gateway connection* sensor shows this with the reason.
- Erasing every recording at once and booking pay-per-view events are deliberately not offered.

## Troubleshooting

- **A box isn't found:** it must be on the same network and subnet as Home Assistant (SSDP doesn't cross VLANs). Add it by IP address instead.
- **A box shows as unavailable:** it's unplugged or in deep standby. Boxes in normal standby stay available and show as off.
- **Channel changes land on the wrong channel:** increase *Delay between key presses* (Settings → Devices & services → Sky Q Control → Configure).
- **No guide data:** check Home Assistant can reach `awk.epgsky.com` and `atlantis.epgsky.com`.
- **A Mini's recording sensors went unavailable after adding the gateway:** recordings now come from the gateway; reload the Mini to remove the duplicates.
- **Anything else:** download diagnostics from the box's device page (serials, card numbers and household IDs are redacted) and open an issue.

## Removing the integration

Settings → Devices & services → Sky Q Control → ⋮ on each box → **Delete**. Then remove *Sky Q Control* in HACS and restart Home Assistant. Nothing is stored on the boxes.

## Repository layout

| Path | What |
|---|---|
| `custom_components/skyq_control/` | The Home Assistant integration |
| `aioskyq/` | Async client library for the box's local API, with its own tests |
| `tools/skyq_probe.py` | Probe that maps everything a box exposes ([docs/PROBE.md](docs/PROBE.md)) |
| `docs/PLAN.md` | Research, API inventory, entity map and phases |
| `tests/` | Integration tests (pytest-homeassistant-custom-component) |

## Development

```bash
uv sync                                   # Home Assistant 2026.10 + test harness + local aioskyq
uv run pytest                             # integration tests (mocked box, real fixtures)
uv run ruff check . && uv run ruff format --check .
SKYQ_GATEWAY=192.168.1.50 SKYQ_MINIS=192.168.1.51 uv run pytest tests/test_live.py -m live -s   # read-only, real boxes
```

## Licence

MIT; see [LICENSE](LICENSE). Not affiliated with or endorsed by Sky. "Sky" and "Sky Q" are trademarks of their owner.
