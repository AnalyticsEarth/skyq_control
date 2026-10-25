# Sky Q Control for Home Assistant

A local-push Home Assistant integration that aims to expose **every control and every piece of metadata** a Sky Q box offers on the local network. It supports gateways and Minis. Sky Glass and Sky Stream use a different platform and are not supported.

> **Status: v0.2.0.** Controls are in: a media player, a remote, a button for every remote key, channel and app pickers, and actions for automations. Sensors, the recordings calendar and the media browser come next. See [docs/PLAN.md](docs/PLAN.md).

## What you get per box

| Entity | What it does |
|---|---|
| **Media player** (`media_player.sky_q_…`) | On/off, play/pause/stop, channel up/down (next/previous), and a channel picker (the box's favourites, or channels 101–199 if it has none). Shows the channel, the current programme with artwork and a live progress bar, the next programme, a recording's title and position, or the app on screen. |
| **Remote** (`remote.sky_q_…`) | Every remote key through `remote.send_command`, with repeats and delays. Works with HA's remote card. |
| **Buttons** | One per remote key. Navigation, transport, colour keys, the red button, Sky, TV guide and info are enabled. Digits, Box Office, Help, Services, Interactive and Text are created disabled; enable them if you want them. |
| **Selects** | **Channel** (favourites) and **App** (installed apps such as YouTube and Netflix). Picking an option tunes the channel or opens the app. |

**Actions for automations** (target a Sky Q media player):
- `skyq_control.send_keys`: keys such as `backup, 1, 0, 1` or `tv_guide`, with an optional delay between them.
- `skyq_control.change_channel`: a channel number such as `101`, or `sid:3008`.
- `skyq_control.launch_app`: an app name or ID such as `YouTube`.

`media_player.play_media` also accepts `channel`, `app` and `keys` as media types.

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
