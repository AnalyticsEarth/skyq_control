# Sky Q Full-Control Integration for Home Assistant: Research & Plan

_Drafted 2026-10-25._

## 1. Goal

Build a Home Assistant custom integration (installable through HACS) that exposes **every control and every piece of metadata** that a Sky Q box offers on the local network. That covers remote keys, playback, channels, recordings (PVR), EPG, storage, system and HDMI health, entitlements, and apps.

## 2. What exists today

| Project | Status | What it gives you | Gaps |
|---|---|---|---|
| [RogerSelwyn/Home_Assistant_SkyQ_MediaPlayer](https://github.com/RogerSelwyn/Home_Assistant_SkyQ_MediaPlayer) (HACS default, domain `skyq`, v3.0.3) | Unmaintained ("maintained: no 2025"; the author no longer owns a box) | One `media_player` entity (on/off, play/pause, channel up/down mapped to FF/RW, a source list built from config, `play_media` that sends key sequences, media browser), plus storage and schedule sensors, device actions, and a template-switch YAML generator | No `remote` entity, no buttons, no PVR management actions, no EPG, no status/HDMI/smartcard/entitlement data, no push updates (it polls), no calendar, no events. It also uses a blocking `requests` library. |
| [RogerSelwyn/skyq_remote](https://github.com/RogerSelwyn/skyq_remote) (`pyskyqremote` 0.3.26) | Unmaintained | The most complete client: TCP remote, SOAP transport/media info, REST device/system/time/apps/services/favourites/PVR, PVR write actions, and EPG from `atlantis.epgsky.com` | Synchronous. It opens a new websocket for each poll, ignores `/as/system/status`, `/as/pin` and `/as/pvr/changes`, and parses only part of each payload. |
| [bradwood/pyskyq](https://gitlab.com/bradwood/pyskyq) | Abandoned (2018) | Async status websocket, channel details with `streamingprofiles` | Limited scope |
| [Sky Q Cloud PVR API spec](https://github.com/davwheat/sky-q-cloud-pvr-api-documentation) (archived Swagger file from Sky) | Reference only | **Sky's own Swagger definition** of the `/as/` API: endpoints, parameters and field schemas | This is an emulator spec; each endpoint still needs checking against a real box |
| [gladdy.uk blog (2017)](https://gladdy.uk/blog/2017/03/13/skyq-upnp-rest-and-websocket-api-interfaces/) | Reference | Ports, the websocket status endpoint, UPnP service names | Old |
| [ParkSquare.SkyTv (.NET)](https://www.parksq.co.uk/dotnet-core/sky-q-remote-control), [sky-remote (node)](https://github.com/dalhundal/sky-remote), [Homey app](https://community.homey.app/t/app-pro-sky-q-release-2-0-0-test-2-0-1/38581) | Deprecated or minimal | They confirm `cancelrecording` and the key codes | Nothing new beyond the above |

**Context:** Sky stopped selling Sky Q online in December 2025 (it is being replaced by Sky Stream and Sky Glass). Firmware is effectively frozen, which is good for API stability. Sky Stream and Sky Glass use a different platform and are **out of scope**.

## 3. The Sky Q local API surface (complete inventory)

There is no official documentation. Confidence levels used below:
- **P**: used in production by pyskyqremote, so it works on real boxes.
- **S**: listed in Sky's own Swagger spec.
- **C**: reported by the community.
- **?**: a hypothesis that the probe (Phase 0) must confirm.

### 3.1 Transports

| Port | Protocol | Purpose |
|---|---|---|
| 49160/tcp (5900 on firmware older than 060) | Raw "SKY 000.001" handshake + 8-byte key packets | Remote-control keys |
| 9006/tcp | HTTP JSON under `/as/…` + websocket upgrade on `…/status` paths | Main REST API and push status |
| 49153/tcp | UPnP / SOAP (`/description{N}.xml`, `SkyPlay:2`, `SkyBrowse`, `SkyControl`) | Transport state, current URI, possibly tuning |
| SSDP | `urn:schemas-nds-com:device:GatewaySkyControl:2`, `…:MRSkyControl:2` | Discovery (gateway and Mini boxes) |
| 4730/tcp | HTTP `.ts` / `.ttml` streams | Encrypted live and PVR streams. **Out of scope** (DRM). |

### 3.2 Remote keys (TCP 49160): all of them

`power(0) select(1) backup/dismiss(2) channelup(6) channeldown(7) interactive/sidebar(8) help(9) services/search(10) tvguide/home(11) i(14) text(15) up(16) down(17) left(18) right(19) red(32) green(33) yellow(34) blue(35) 0–9(48–57) play(64) pause(65) stop(66) record(67) fastforward(69) rewind(71) boxoffice(240) sky(241)`. **P**

**Undocumented-code sweep** (Mini 192.168.1.52, 2026-10-25), covering 3–5, 12–13, 20–31, 36–47, 58–63, 68, 70, 72–95, 224–239 and 242–255:
- **12 = red button.** It launches the current channel's interactive service; on Sky Sports Main Event that is the Sky Sports Hub (`com.sky.sports.six`). Expose it as `redbutton`.
- **Some codes in 224–255** open the remote-control setup menu. These are deliberately not exposed.
- Every other swept code did nothing visible. Volume and mute are not on this interface (they go over IR/Bluetooth to the TV). Codes 96–223 were not swept.

### 3.3 REST, read side (port 9006, GET)

| Path | Data | Conf. |
|---|---|---|
| `/as/system/deviceinformation` | ASVersion, IPAddress, countryCode, gateway, hardwareName, modelNumber, serialNumber, versionNumber, bouquet, subbouquet, householdToken, pipCapable, streamingCapable | P, S |
| `/as/system/information` | activeStandby, deviceType (GATEWAYSTB/MULTIROOMSTB/…), hardwareModel, manufacturer, gatewayIPAddress, wakeReason, systemUptime, hdrCapable, uhdCapable, MAC and more (parse the whole payload) | P |
| `/as/system/time` | utc, presentLocalTimeOffset, futureLocalTimeOffset, futureTransitionUtc | P |
| `/as/services/{bouquet}/{subbouquet}` (also `/as/services`) | Channel list: c (number), t (name), sid, sf (sd/hd/uhd/au), sg, xsg, sk, servicetype, dvbtriplet, schedule, adult, avail, local | P, S |
| `/as/services/details/{sid}` | dvbtriplet, isbroadcasting, upgradeMessage (description), streamingprofiles[] | C |
| `/as/services/favourites` | Favourites list (lcn, sid) | P |
| `/as/apps` | Installed apps (appId, title, …) | P |
| `/as/pvr?limit=&offset=` | pvrItems[] + documentId, version, totalPvrItems | P, S |
| `/as/pvr/details/{pvrid}` | A single recording (full field set below) | P, S |
| `/as/pvr/changes/{documentId}/{version}` | **Incremental PVR diff**, so we avoid re-pulling 1,000 items | S, ? |
| `/as/pvr/storage` | userQuotaMax, userQuotaUsed | P, S |
| `/as/pin` | PIN subsystem status (not the PIN itself) | S, ? |
| `/as/schedule/{yyyymmdd}/{sid}`, `/as/schedule/event/{eid}` | EPG on the box itself (pyskyqremote uses the Sky cloud host `atlantis.epgsky.com` instead) | S, ? |

**PVR item fields** (from Sky's spec): `pvrid, status, t (title), sy (synopsis), cn (channel name), c, osid, oeid, osk, st (start), schd (scheduled duration), ast (actual start), finald, et, lp/lpt (last played position/time), lto, del (delete time), fr (failure reason), src, type, keep, lock, link (series link), hd, uhd, s, w, slo, canl, at, eg/esg (genre), seasonnumber, episodenumber, seasonuuid, seriesuuid, programmeuuid, predictedsz, streamuri, tso`.

**EPG event fields:** `eid, t, sy, st, d, seasonnumber, episodenumber, programmeuuid, seasonuuid, seriesuuid, r (rating), hd, ad (audio description), s (subtitles), new, restartable, canb (can book), canl, ippv/oppv, slo, w, eg/esg (genre), cgid, haschildren, hasAlternativeAudio, at, tso`.

### 3.4 Websockets (port 9006): push, no polling needed

| Path | Pushes |
|---|---|
| `/as/system/status` | `camessage{state,reason}`, `drmstatus`, **`entitlements[]`** (HD, ULTRA+, SKY+, BIGBASIC, …), `epginfobits`, `gatewayservices`, **`hdmi{state, reason, authenticatedHDCP, sinkHDCP, sinkUHD, sinkHLG, 2160p10bitCapable, uhdConfigured}`**, `network`, `nssplayback`, `pvr`, `schedule{lastdate}`, `servicelist`, **`smartcard{active, paired, bouquet, countryCode, currency, householdid, transactionlimit, viewingCardNumber, cwe}`**, `swupdate{state, reason}`, `systemupdates{…counters}`, `updatetask` (C) |
| `/as/apps/status` | apps[] with `appId` + `status` (VISIBLE = foreground) (P) |
| other `…/status` paths, e.g. `/as/pvr/status`, `/as/services/status` | **?** The probe tries every resource root. A PVR push would make recording events instant. |

### 3.5 SOAP / UPnP (port 49153)

| Action | Data | Conf. |
|---|---|---|
| `GetTransportInfo` | CurrentTransportState (PLAYING/PAUSED_PLAYBACK/STOPPED/TRANSITIONING/NO_MEDIA_PRESENT), Status, Speed (trick-play speed) | P |
| `GetMediaInfo` | CurrentURI: `xsi://{SID hex}` for live TV, `file://pvr/{id}` for a recording | P |
| `GetPositionInfo` | RelTime/TrackDuration, giving a position bar for recordings | ? |
| `SetAVTransportURI(xsi://SID)` | **Direct channel tune by SID** (works on Sky+HD; the 2017 report says Sky Q returns "TRANSPORT IS LOCKED") | ? |
| `Play/Pause/Seek/Stop` | Direct transport control | ? |
| `SkyBrowse` ContentDirectory `Browse` | Recordings listing (Sky+HD heritage) | ? |
| `description{N}.xml` | Full service list. Enumerate it all in the probe. | P |

### 3.6 REST, write side (POST unless noted)

`/as/pvr/action/bookrecording?eid=` · `bookseriesrecording?eid=` · `bookppvrecording?eid=&offerref=(&pin=)` · `cancelrecording?pvrid=` · `delete` · `undelete` · `erase` · `keep` · `unkeep` · `lock` · `unlock` · `serieslink` · `seriesunlink` · `setlastplayedposition?pos=&pvrid=` (the path differs between pyskyqremote and the spec, so probe both) · `DELETE /as/pvr` (erase everything) · `PUT /as/pvr/details/{pvrid}` (edit an item) · `POST /as/pin/action/submit?pin=` · `POST /as/system/action/ping`.

Candidates to probe for **?**: app launch (`/as/apps/{appId}/launch`, `/as/apps/action/launch?appId=`), standby/wake actions under `/as/system/action/*`, and tuning under `/as/services/action/*`.

### 3.7 Cloud metadata and EPG (no account needed, verified 2026-10-25)

The box has no on-box guide, so the EPG comes from Sky's public metadata hosts. These are the same endpoints Sky's own apps and the [iptv-org `sky.com` grabber](https://github.com/iptv-org/epg/blob/master/sites/sky.com/sky.com.config.js) use. They are unofficial and undocumented, so build in caching and a fallback.

| Endpoint | Use | Notes |
|---|---|---|
| `GET https://awk.epgsky.com/hawk/linear/schedule/{yyyymmdd}/{sid[,sid…]}` + `X-SkyOTT-Territory: GB` | **Primary EPG** | Up to 20 SIDs per request (all 377 channels take 19 requests, ~9 s). Covers yesterday through **today + 8 days**. The date is a **UTC** day: also fetch day+1 to cover local evenings. Fields match Sky's `LinearEvent` schema: eid, t, sy, st (UTC epoch), d, seasonnumber, episodenumber, programmeuuid, seriesuuid, r, s, ad, hd, new, canb, canl, restartable, ippv/oppv, eg/esg, cgid, haschildren, hasAlternativeAudio, slo, w, signed. |
| `GET https://atlantis.epgsky.com/as/schedule/{yyyymmdd}/{sid}` + `X-SkyOTT-Proposition: SKYQ`, `X-SkyOTT-Provider: SKY`, `X-SkyOTT-Territory: GB` | **UHD channels only** (hawk returns nothing for them) | One SID per request, about 7 days ahead. Without the headers it returns HTTP 400, which is why pyskyqremote's EPG broke. |
| `GET https://awk.epgsky.com/hawk/linear/services/{bouquet}/{subbouquet}`, `…/hawk/linear/regions` | Channel list and regions (cross-check against the box's own list) | Same headers as the schedule |
| `https://images.metadata.sky.com/pd-image/{programmeuuid}/16-9/{640\|1024}` | Programme art | 3-4 ratio returned 404 |
| `https://d2n0069hmnqmmx.cloudfront.net/epgdata/1.0/newchanlogos/600/600/skychb{sid}.png` | Channel logo (needs only the SID) | `imageservice.sky.com/logo/skychb_{sid}{alnum lowercase name}/600/600?territory=GB&provider=SKY&proposition=SKYQ` also works |

- **Coverage on your gateway** (bouquet 4101/1): hawk covered 367 of 377 channels and atlantis added the 3 UHD channels. The only 7 channels with no data are the adult channels (900–908).
- **Event IDs match the box.** hawk's `eid` uses the same `E{sid hex}-{event hex}` format as PVR `oeid` (for example `Ebc0-39e9` on SID 3008 = 0xBC0), so guide events can feed `pvr/action/bookrecording?eid=` directly. Confirm with `--book-eid`.
- **Territories:** use `X-SkyOTT-Territory` GB (also for Ireland), DE or IT.
- **Fallback:** an optional user-supplied XMLTV URL (for example iptv-org output, or [dp247/Freeview-EPG](https://github.com/dp247/Freeview-EPG) for free-to-air channels only), matched by channel number and name. Use it if Sky changes these hosts.

### 3.8 Probe findings (2026-10-25, read-only run)

The boxes probed were one gateway (ES140, `192.168.1.50`) and three Minis (EM150: `.51`, `.52`, `.53`). Raw data is in `probe_results/`.

- **Confirmed on all boxes:** remote handshake on 49160; `deviceinformation`, `information`, `time`, `apps`, `services`, `favourites`, `pvr` (454 items), `pvr/storage`, `pin`, `pvr/changes/{doc}/{ver}`, `services/details/{sid}`, `system/action/ping`.
- **New:** `GET /as/services/genres` (genre table: sg, xsg, t).
- **Push websockets:** `system/status`, `apps/status`, **`pvr/status`** (pushes `{documentId, min, max}`, which pairs with `pvr/changes`) and **`services/status`** all send messages. The box also accepts an upgrade on *any* `…/status` path but never sends anything there, so count a stream only if it delivers messages. The gateway sometimes stalls a socket after its first message, so the client needs a watchdog and reconnect.
- **SOAP:** every box exposes `GetMediaInfo(_Ext)`, `GetTransportInfo`, `GetPositionInfo`, `GetTransportSettings`, `GetCurrentTransportActions`, plus **Play/Pause/Stop/Seek/Next/Previous/SetAVTransportURI** in SCPD. The gateway also has `SkyBook`, `SkyBrowse` and `BasicManagement`. Its SkyServe device repeats SkyPlay with a control URL that rejects every action, so pick the service by a working call. Live position reads `00:00:00`; check it during recording playback.
- **SkyBrowse (gateway only):** containers `tuner`, `epg`, `pvr` (454), `nds_pvr_items` (997), `recycle`, `nds_resources_items`.
- **PVR statuses seen:** RECORDED, PART REC, FAILED. Sources: LIVE, VOD, EST3. Fields beyond the spec: `downloadlink`, `vodprogrammeid`, `carating`, `exp`, `marketingmessage`, `imageuri_16x9/3x4`, `finalsz`, `gap`, `sl`, `au`, `3d`.
- **Extra `system/information` fields:** CAType, DRMActivationStatus, airplayID, btID, cable, AdSmart versions, localIRDatabase, meshEnabled, networkVersion, numDTTTuners, purchasebalance/limit, receiverID, vespaID, deviceID.
- **Extra `system/status` sections:** `systemstate`, and `systemupdates` counters (applaunchrequest, netflix, downloadqueue, hdmi, messages…).
- **Not available:** the on-box EPG (`/as/schedule/...` returns 404). The atlantis 400 was caused by missing `X-SkyOTT-*` headers. **EPG source resolved in §3.7:** hawk is primary and atlantis covers UHD.
- **Write tests** (Mini 192.168.1.52, test recording P2902aeb9, test booking on E81b-15a0):
  - **SOAP transport is locked.** `Pause` and `SetAVTransportURI` return UPnP 705 `TRANSPORT IS LOCKED`, and `Play` returns 200 but does nothing. Playback and channel changes must use **remote keys** (digits for a channel number). The SOAP *read* actions remain the state source.
  - **PVR actions work through a Mini** (it proxies to the gateway): keep/unkeep, lock/unlock, serieslink/seriesunlink, delete/undelete, `bookrecording?eid=` with a hawk EPG eid. The booking response body is empty, so find the new item by `oeid` in the PVR list.
  - **`delete` on a SCHEDULED item cancels the booking.** It takes effect a few seconds later.
  - **`setlastplayedposition` is forward-only.** `pvr/action/setlastplayedposition?pos=&pvrid=` moves the resume point forward. Moving it backwards returned 500, then the request hung. The integration must never send a lower position. The spec's `/as/pvr/setlastplayedposition` path returns 404.
  - **App launch works: `POST /as/apps/action/launch?appId={appId}`** (for example `YouTube`, `Netflix`). App ids come from `/as/apps`. The other five guessed paths return 404.
- **Listen mode (push vs poll):**
  - App changes are pushed instantly on `apps/status`.
  - Standby and wake are pushed on `system/status` (`systemstate`, `gatewayservices`, `systemupdates.hdmi`).
  - **Channel changes are not pushed.** They only appear by polling SOAP `GetMediaInfo` (`xsi://` URI) and `GetTransportInfo` (PLAYING → TRANSITIONING → PLAYING). Poll every 2–5 s while the box is on.
- **Not yet observed:** `pvr/status` push timing when a recording starts or finishes, playback position during a recording, and the remaining codes 96–223.

### 3.9 Phase 1 findings (aioskyq against real boxes, 2026-10-25)

- **Favourites are per box.** The gateway has its own list and the Minis have none, so read them from each box.
- **Minis don't stream.** `services/details/{sid}` returns `isbroadcasting: false` and no `streamingprofiles` on a Mini, while the gateway returns both.
- **When an app is on screen**, `GetMediaInfo.CurrentURI` is empty and `apps/status` names the visible app (for example `YouTube`). Now-playing should fall back to the app.
- **Channel numbers are not unique.** 401 is both Sky Sports Main Event HD and its UHD variant. Resolve by SID wherever possible, and when looking up a number prefer the variant the box can tune (UHD on UHD boxes).

## 4. Architecture

**Phase 2 status:** done (2026-10-25). `custom_components/skyq_control` covers:
- **Config flow:** SSDP discovery with confirm, a user scan or manual IP entry, reconfigure, and options (key spacing, poll interval).
- **Per-box coordinator:** 5 s poll when on and 30 s in standby, push merging, and recordings fetched once per household.
- **Device registry:** model, firmware, serial and MAC per box, with Minis linked via `via_device_id`.
- **Diagnostics** with identifiers redacted.

It was tested against HA 2026.10.4: 34 mocked tests and a live test on all four boxes.

**HA 2026.10 changes picked up:**
- `device_registry.async_get_device` is deprecated in favour of `async_get_device_by_identifier(identifier, config_entry_id)`, because identifiers are only unique per entry.
- `DeviceInfo.via_device` is deprecated in favour of `via_device_id`.
- `DeviceEntry.config_entries` is deprecated in favour of `config_entry_id`.

**Notes for Phase 3:**
- Coordinators only poll while entities listen, so the skeleton itself doesn't poll.
- On live TV the visible app is `com.bskyb.epgui` (Sky's TV interface), which should count as "watching TV", not as an app.

**Phase 1 status:** done. The `aioskyq/` package is a standalone library intended for PyPI, because Home Assistant installs integration requirements from PyPI. Its quirks and API are documented in `aioskyq/README.md`.

```
custom_components/skyq_control/         # new domain: `skyq` is taken by the old integration, so both can coexist
  manifest.json  (ssdp matchers, iot_class: local_push, integration_type: device)
  __init__.py    (entry setup, runtime_data, services)
  coordinator.py (SkyQCoordinator: push + tiered polling)
  config_flow.py (SSDP discovery, manual host, options)
  entity.py, media_player.py, remote.py, button.py, select.py,
  sensor.py, binary_sensor.py, calendar.py, image.py, event.py,
  device_trigger.py, diagnostics.py, services.yaml, strings.json, icons.json
  lib/aioskyq/   (vendored async client; split out to PyPI later)
    remote.py  (TCP keys, asyncio streams, per-box send lock)
    rest.py    (aiohttp)
    ws.py      (persistent websockets with reconnect/backoff)
    soap.py    (UPnP description discovery + actions)
    epg.py     (hawk batches of 20 SIDs, atlantis for UHD, optional XMLTV fallback; cached per sid/UTC day)
    models.py  (dataclasses keep *all* raw fields + typed accessors)
```

**Why a new async library instead of pyskyqremote:** pyskyqremote is synchronous and unmaintained, opens a new websocket for each poll, and drops most payload fields. HA's rules favour async I/O. Our models will keep the raw dicts so that no metadata is ever thrown away.

**Update strategy:**

| Tier | Source | Cadence |
|---|---|---|
| Push | `/as/system/status` WS, `/as/apps/status` WS | Instant |
| Fast | SOAP `GetTransportInfo` + `GetMediaInfo` (+ `GetPositionInfo`) | 5 s while on; 30 s in standby |
| Medium | `/as/system/information` (standby, uptime), `/as/pvr/changes` | 30–60 s |
| Slow | Channels, favourites, apps, storage, device info, time | 15–60 min, or on demand |
| EPG | Now/next for the current channel when the channel changes. Favourites refreshed every 6 h (one batched hawk call per 20 channels). Full 8-day grid on request. | Cached per SID and UTC day |

**Multiroom:** The gateway is the parent device and Mini boxes link to it with `via_device`. PVR, storage and calendar entities live on the gateway only. Unsupported device types (`IPSETTOPBOX`, `TV`) are rejected with a clear error.

## 5. Entity and control map: "show everything"

### Controls
| Platform | Entities |
|---|---|
| `media_player` | State (off/standby/on/playing/paused/buffering), on/off, play/pause/stop, FF/RW (`media_seek` mapped to repeated presses if SOAP Seek fails), channel up/down as next/previous, `select_source` (favourites, then all channels, then apps), `play_media` (channel number, `sid:`, key sequence, `pvr:` if tuning is supported), browse media (Favourites / All channels by genre / Recordings / Now & Next / Apps). Attributes: channel, number, sid, programme title, season/episode, synopsis, artwork, start/end, media position, live or recording, pvrid, transport speed |
| `remote` | `send_command` with all 45 keys, `num_repeats`, `delay_secs`, `hold_secs`; also on/off. Works with HA's built-in remote card. |
| `button` | One per key (≈45). Navigation, colour, transport and power keys are enabled by default; the rest are disabled by default. |
| `select` | Favourite channel picker, all-channel picker (disabled by default), **app launcher** (`POST /as/apps/action/launch?appId=`, confirmed) |
| `calendar` | **Scheduled recordings** (title, channel, synopsis, series link) and **Completed recordings**. Optional EPG calendar per favourite channel (opt-in, because it is heavy). |
| `image` | Current programme artwork and current channel logo |
| `event` | `recording_started`, `recording_finished`, `recording_failed`, `channel_changed`, `app_changed` |

### Metadata
| Platform | Entities (diagnostic category unless noted) |
|---|---|
| `sensor` (primary) | Current channel, current programme, next programme, programme end time, active app, transport state, storage used % / GB used / GB free / quota, recordings count by status (recorded, scheduled, recording, deleted/trash, failed), next scheduled recording (timestamp), currently recording (titles in attributes) |
| `sensor` (diagnostic) | Software version (ASVersion), firmware build (versionNumber), model number, hardware name and model, device type, serial number, uptime (timestamp of boot), wake reason, gateway IP, bouquet/sub-bouquet, country, UTC offset, next DST transition, EPG data available until (`schedule.lastdate`), HDMI state/reason, HDCP authenticated, sink HDCP version, software-update state/reason, update task, CA message, PIN subsystem state, entitlements (count + list in attributes), smartcard state, household ID and viewing card number (**disabled by default, redacted in diagnostics**) |
| `binary_sensor` | Power (on/standby), recording in progress, HDMI connected, sink UHD capable, sink HLG capable, UHD configured, 2160p10bit capable, HDR capable, UHD capable, PiP capable, streaming capable, smartcard active/paired, DRM available, network OK, PVR subsystem OK, software update pending, watching a recording vs live, adult channel |

### Actions (services), all targeting a device or entity
`send_keys(sequence, delay)` · `change_channel(number|sid)` · `book_recording(eid, series)` · `book_ppv_recording(eid, offerref)` · `cancel_recording` · `delete_recording` / `undelete_recording` / `erase_recording` · `erase_all_recordings` (needs `confirm: true`) · `keep` / `unkeep` · `lock` / `unlock` · `series_link` / `series_unlink` · `set_last_played_position(pvrid, pos)` · `record_now` (books the current programme by eid) · `refresh`.

**Response-returning actions** (HA `SupportsResponse.ONLY`) for scripts and LLM agents: `get_epg(sid|number, date, days)`, `search_epg(title, days)` (then book), `get_recordings(status, limit)`, `get_recording(pvrid)`, `get_channels()`, `get_favourites()`, `get_apps()`, `get_system_status()` (raw).

### Automation hooks
Device triggers: turned on, turned off, channel changed, app changed, recording started, recording finished, recording failed, storage above X %. Each fires on the HA bus too (`skyq_control_event`).

### Dashboard
Ship a ready-made dashboard (YAML in `docs/dashboard.yaml`) that has a full remote layout (the D-pad, colour keys, number pad and transport keys built from button entities), now-playing info, a storage gauge, the recordings calendar and an EPG panel. A dedicated Lovelace card in its own repo can come later.

## 6. Delivery phases

| Phase | Work | Done when |
|---|---|---|
| **0. Probe (needs a real box)** | `tools/skyq_probe.py`: enumerate the UPnP descriptions; call every GET in §3.3; open each websocket candidate; try each SOAP action; sweep key codes interactively; try candidate write endpoints **only against a test recording**. Save the redacted JSON to `tests/fixtures/`. | Every **?** item is marked as works or doesn't, and a fixture exists for each payload |
| 1. `aioskyq` library | Async client and models built from the fixtures; pytest with mocked aiohttp/TCP | ≥90 % coverage; runs against a live box from the CLI |
| 2. Integration skeleton | Config flow (SSDP + manual + options), coordinator, device registry (gateway/Mini), diagnostics with redaction, translations | Box discovered and added; diagnostics download works |
| 3. Controls | `media_player`, `remote`, `button`, `select`, `send_keys`/`change_channel` actions | Every key can be pressed from the UI |
| 4. Metadata | All sensors and binary sensors from §5, plus `image` | Every field in §3 maps to an entity or attribute |
| 5. PVR & EPG | PVR actions, calendar, response actions, browse media, now/next | Booking, keeping, locking and deleting work end to end |
| 6. Events & polish | Event entities, device triggers, dashboard YAML, HACS packaging (`hacs.json`, hassfest + HACS GitHub Actions), README, quality-scale checklist | Passes hassfest and HACS validation; tagged release |

## 7. Risks and decisions

- **Hardware access:** Every **?** in §3 needs a live box. Phase 0 blocks the rest, and Mini boxes need testing too.
- **Domain name:** I propose `skyq_control` so it can be installed next to the old `skyq` integration. A migration helper that imports the old config entries is optional.
- **Destructive actions** (`erase`, `erase_all`): require explicit confirmation fields, and the probe never runs them on real recordings.
- **Sensitive data** (viewing card number, household ID/token, serial number): entities are disabled by default and the values are redacted in diagnostics and logs.
- **PIN entry / PPV:** The PIN is stored in the config entry and is never logged. PPV booking spends real money, so it stays a manual action and is never added to the UI.
- **Direct tuning (confirmed locked):** `SetAVTransportURI` and SOAP Pause return 705 `TRANSPORT IS LOCKED`. Channel changes type digits (backup + digits), which is reliable but slower, and play/pause/seek use keys.
- **Resume position:** forward-only. Never send a position earlier than the current `lp`, because the box returns 500 and then hangs.
- **Remote reliability:** The box drops presses that arrive too fast. Serialise sends per box with a 0.3–0.5 s gap (configurable).
- **Port quirks:** Firmware older than 060 uses port 5900. Make the port configurable and auto-detect it.

## Sources
- [pyskyqremote source](https://github.com/RogerSelwyn/skyq_remote) · [existing HA integration](https://github.com/RogerSelwyn/Home_Assistant_SkyQ_MediaPlayer)
- [Sky Q Cloud PVR API Swagger archive](https://github.com/davwheat/sky-q-cloud-pvr-api-documentation)
- [gladdy.uk: Sky Q UPnP, REST and WebSocket API interfaces](https://gladdy.uk/blog/2017/03/13/skyq-upnp-rest-and-websocket-api-interfaces/)
- [pyskyq docs](https://bradwood.gitlab.io/pyskyq/api/pyskyq.html) · [ParkSquare .NET client](https://www.parksq.co.uk/dotnet-core/sky-q-remote-control) · [sky-remote issue #11 (port change)](https://github.com/dalhundal/sky-remote/issues/11) · [sky-q issue #1](https://github.com/dalhundal/sky-q/issues/1)
- [HA community thread](https://community.home-assistant.io/t/custom-component-skyq-media-player/140306?page=41) · [Wikipedia: Sky Q](https://en.wikipedia.org/wiki/Sky_Q)
