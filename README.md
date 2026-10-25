# Sky Q Control for Home Assistant

A local-push Home Assistant integration that aims to expose **every control and every piece of metadata** a Sky Q box offers on the local network. It supports gateways and Minis. Sky Glass and Sky Stream use a different platform and are not supported.

> **Status: Phase 2 (skeleton).** Discovery, setup, the per-box data coordinator, devices and diagnostics work against real boxes. Entities (media player, remote, buttons, sensors, the recordings calendar) arrive in Phases 3 to 5. See [docs/PLAN.md](docs/PLAN.md).

## What works now

- **Discovery:** boxes are found automatically over SSDP, or you can pick from a network scan or type an IP address.
- **One device per box:** each box shows its model (ES240 gateway, EM150 Mini), firmware, serial number and MAC address. Minis are linked to their gateway.
- **Live state:** playback is polled every 5 s while the box is on (30 s in standby), because the box doesn't push channel changes. System status, app changes, recording changes and channel-list changes arrive by push over the box's websockets.
- **Recordings** are fetched once per household, by the gateway.
- **Programme guide** comes from Sky's public metadata service, because the box itself has no guide.
- **Options:** the delay between key presses and the playback poll interval.
- **Reconfigure** an entry when a box gets a new IP address.
- **Diagnostics** download with serials, card numbers and household IDs redacted.

## Install (development)

The integration depends on [`aioskyq`](aioskyq/), which isn't on PyPI yet. Until it is, install it into Home Assistant's Python environment yourself (for example `pip install ./aioskyq`), then copy `custom_components/skyq_control` into your Home Assistant `config/custom_components/` folder and restart.

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
