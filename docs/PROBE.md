# Phase 0 probe: `tools/skyq_probe.py`

This script maps everything a Sky Q box exposes on the LAN and answers the open (**?**) questions in [PLAN.md](PLAN.md). It needs only [uv](https://docs.astral.sh/uv/), which installs `aiohttp` for you through the script's inline metadata. Run it from a machine on the same network as the box.

## 1. Read-only run (safe, takes about 1 minute)

```bash
uv run tools/skyq_probe.py 192.168.1.50
```

If you leave out the IP, it uses SSDP to discover every gateway and Mini box on the network and probes each one. Turn the box **on** (not in standby) and leave it on live TV before you run it.

This run does not change anything on the box:
- It connects to the remote-control port and reads the `SKY` banner. No key is sent.
- It calls every known REST GET plus ~35 undocumented candidate paths.
- It fetches the UPnP descriptions and SCPDs, then calls every SOAP `Get*`/`Browse` action that takes only safe arguments.
- It listens for 15 s on 15 websocket candidates.
- It posts `system/action/ping`, which is a liveness check.
- It fetches one day of EPG for the current channel from Sky's cloud host. Use `--no-cloud` to skip this.

## 2. Listen mode (no changes from the script)

```bash
uv run tools/skyq_probe.py 192.168.1.50 --listen 180
```

While this runs, use the **real remote**: change channel, pause and resume, play and seek a recording, start a recording, open an app, then go to standby and wake the box. The script logs every websocket push and every change in polled state with timestamps. This shows which events we can get by push and which need polling.

## 3. Key sweep (interactive, sends keys)

```bash
uv run tools/skyq_probe.py 192.168.1.50 --key-sweep
```

The script steps through undocumented key codes (by default `3-5,12-13,20-31,36-47,58-63,68,70,72-95,224-239,242-255`). For each one you press Enter to send it, `s` to skip or `q` to quit. Then type what you saw on the TV. It also records any change in transport, channel or visible app automatically. Use `--codes 3-10` to narrow the range. Keep the real remote handy to back out of menus.

### Key batch (non-interactive)

```bash
uv run tools/skyq_probe.py 192.168.1.50 --key-batch 72-95 --gap 4
```

This sends each code `--gap` seconds apart and logs any change in channel, transport or app. After each code it presses Back twice to close any menu. Use it when nobody can type answers at a prompt; someone watching the TV notes what appears.

> **macOS:** the Claude app's Terminal panel may be blocked from your local network (Privacy & Security → Local Network). In that case every request shows `None`/`ClientConnectorError` and the remote port shows "closed". Run the probe from a terminal that has Local Network access instead.

## 4. Write tests (changes box state, asks first)

Pick a **recording you don't care about** and take its `pvrid` from `fixtures/rest/pvr_list.json`.

```bash
uv run tools/skyq_probe.py 192.168.1.50 --writes --pvrid P12345abc
```

| Flag | What it does | Reversed? |
|---|---|---|
| `--writes` (always) | SOAP Pause → Play on live TV. SOAP `SetAVTransportURI` re-tunes to the **current** channel, so it tests whether the transport is locked. | Yes |
| `--pvrid` | keep/unkeep, lock/unlock, series link/unlink (scheduled items only) | Yes. Each toggle is put back straight away |
| `--pvrid` | Moves the resume position **5 s forward** | **No.** The box rejects moving it backwards (500, then the request hangs) |
| `--allow-delete` | delete, then undelete (the recording goes to trash and back) | Yes |
| `--book-eid EID` | Books one recording, then series links and unlinks it. With `--allow-delete` it also cancels the booking. | Only with `--allow-delete`. Otherwise remove the booking with the remote. |
| `--tune-sid SID` | Tunes to a **different** channel through SOAP instead of re-tuning the current one | Changes the channel |
| `--app-launch APP_ID` | Tries 6 candidate app-launch endpoints (take app ids from `fixtures/rest/apps.json`) | Opens the app if one works |

The script never calls `erase`, `DELETE /as/pvr`, `PUT /as/pvr/details`, the PIN submit endpoint or PPV booking.

## Reading the results

- **Websockets:** the box accepts an upgrade on any `/as/…/status` path, so "Connected ✅" with 0 messages means the path does nothing. Only paths with messages count. If a socket stalls, it is cancelled after `--ws-seconds + 25` s and whatever it already received is kept.
- **SOAP:** the gateway lists SkyPlay twice, and the copy on the SkyServe device rejects every action. The probe uses whichever service answers `GetTransportInfo`.

## Output

`probe_results/<host>_<timestamp>/`
- `report.md`: device summary, the hypothesis checklist (✅/❌/⚪), and tables of every endpoint, SOAP action, websocket and field inventory
- `results.json`: the same data in machine-readable form
- `fixtures/`: raw responses (`rest/`, `soap/`, `upnp/`, `ws/`, `listen/`, `cloud/`). These become the test fixtures for the `aioskyq` library.

Serial numbers, viewing card number, household ID/token and MAC address are **redacted** in every output file. Check the output before you share it anyway: recording titles and channel lists are left in.

## Sending results back

Share `report.md`, or zip the whole timestamped folder. The checklist at the bottom of the console output is the quickest summary.
