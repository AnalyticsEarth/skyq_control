#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["aiohttp>=3.9"]
# ///
"""Phase 0 probe for Sky Q boxes.

Maps everything a Sky Q box exposes on the LAN (REST on 9006, websockets,
UPnP/SOAP on 49153, remote-control TCP on 49160/5900) and saves redacted
fixtures plus a report that answers the open questions in docs/PLAN.md.

Default run is read-only:

    uv run tools/skyq_probe.py 192.168.1.50
    uv run tools/skyq_probe.py                # SSDP discovery, probes every box found

Opt-in modes (each can change box state, see docs/PROBE.md):

    --listen 180          watch push/poll changes while you use the real remote
    --key-sweep           interactively send undocumented key codes
    --writes --pvrid P..  exercise PVR write endpoints on ONE test recording
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import socket
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape

import aiohttp

VERSION = "0.1.0"

REST_PORT = 9006
UPNP_PORT = 49153
REMOTE_PORTS = (49160, 5900)
UPNP_USER_AGENT = "SKYPLUS_skyplus"
SSDP_ADDR = ("239.255.255.250", 1900)
SSDP_TARGETS = (
    "urn:schemas-nds-com:device:GatewaySkyControl:2",
    "urn:schemas-nds-com:device:MRSkyControl:2",
)
# Sky's current EPG host; atlantis.epgsky.com now needs X-SkyOTT-* headers and only adds UHD channels.
CLOUD_SCHEDULE_URL = "https://awk.epgsky.com/hawk/linear/schedule/{date}/{sid}"
CLOUD_HEADERS = {"X-SkyOTT-Territory": "GB"}

KNOWN_KEYS = {
    "power": 0, "select": 1, "backup": 2, "channelup": 6, "channeldown": 7,
    "interactive": 8, "help": 9, "services": 10, "tvguide": 11, "i": 14,
    "text": 15, "up": 16, "down": 17, "left": 18, "right": 19, "red": 32,
    "green": 33, "yellow": 34, "blue": 35, "0": 48, "1": 49, "2": 50, "3": 51,
    "4": 52, "5": 53, "6": 54, "7": 55, "8": 56, "9": 57, "play": 64,
    "pause": 65, "stop": 66, "record": 67, "fastforward": 69, "rewind": 71,
    "boxoffice": 240, "sky": 241,
    "redbutton": 12,  # found by the 2026-10-25 key sweep
}
# Gaps next to known codes are the likeliest homes for undocumented keys.
DEFAULT_SWEEP = "3-5,12-13,20-31,36-47,58-63,68,70,72-95,224-239,242-255"

WS_CANDIDATES = [
    "system/status", "apps/status", "pvr/status", "services/status",
    "pin/status", "schedule/status", "system/time/status", "settings/status",
    "network/status", "display/status", "audio/status", "favourites/status",
    "services/favourites/status", "remote/status", "power/status",
]
EXPLORE_GETS = [
    "", "system", "apps/status", "system/status", "pvr/status",
    "services/status", "pin", "settings", "system/settings", "system/network",
    "network", "display", "system/display", "audio", "system/audio", "remote",
    "power", "system/power", "system/standby", "system/capabilities",
    "parental", "system/parental", "epg", "search", "vod", "ondemand",
    "downloads", "pvr/downloads", "favourites", "services/categories",
    "services/genres", "system/action", "pvr/action", "apps/action",
]
APP_LAUNCH_CANDIDATES = [
    "apps/action/launch?appId={app}",  # confirmed on a Mini; the rest returned 404
    "apps/{app}/launch", "apps/{app}/action/launch", "apps/launch?appId={app}",
    "apps/{app}/start", "apps/action/start?appId={app}",
]

SENSITIVE_SUBSTRINGS = ("serial", "household", "viewingcard", "cardnumber", "macaddress", "token",
                        "deviceid", "receiverid", "vespaid", "airplayid", "btid")
SENSITIVE_EXACT = {"mac", "pin", "udn"}
REDACTED = "**REDACTED**"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", text).strip("_") or "root"


def strip_ns(root: ET.Element) -> ET.Element:
    for el in root.iter():
        if isinstance(el.tag, str) and "}" in el.tag:
            el.tag = el.tag.split("}", 1)[1]
        for key in list(el.attrib):
            if "}" in key:
                el.attrib[key.split("}", 1)[1]] = el.attrib.pop(key)
    return root


def text_of(el: ET.Element | None, name: str) -> str | None:
    if el is None:
        return None
    child = el.find(name)
    return child.text.strip() if child is not None and child.text else None


def is_sensitive(key: str) -> bool:
    k = key.lower()
    return k in SENSITIVE_EXACT or any(s in k for s in SENSITIVE_SUBSTRINGS)


def collect_secrets(obj: Any, out: set[str]) -> None:
    if isinstance(obj, dict):
        for key, val in obj.items():
            if is_sensitive(key) and isinstance(val, (str, int)) and len(str(val)) >= 4:
                out.add(str(val))
            collect_secrets(val, out)
    elif isinstance(obj, list):
        for item in obj:
            collect_secrets(item, out)


def redact_obj(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: (REDACTED if is_sensitive(k) and not isinstance(v, (dict, list)) else redact_obj(v))
                for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact_obj(i) for i in obj]
    return obj


def looks_ok(status: int | None, parsed: Any) -> bool:
    if status is None or not 200 <= status < 300:
        return False
    if isinstance(parsed, dict) and any(k.lower() in ("errorcode", "error", "errordescription") for k in parsed):
        return False
    return True


def summarise(parsed: Any, text: str | None) -> str:
    if isinstance(parsed, dict):
        keys = list(parsed)
        more = f" (+{len(keys) - 12})" if len(keys) > 12 else ""
        return "keys: " + ", ".join(keys[:12]) + more
    if isinstance(parsed, list):
        return f"list[{len(parsed)}]"
    if text:
        return text.strip().replace("\n", " ")[:120]
    return ""


def parse_codes(spec: str) -> list[int]:
    codes: list[int] = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            lo, hi = (int(x) for x in part.split("-", 1))
            codes.extend(range(lo, hi + 1))
        else:
            codes.append(int(part))
    return [c for c in codes if 0 <= c <= 255]


def discover(timeout: float = 4.0) -> list[str]:
    """SSDP M-SEARCH for Sky Q gateway and Mini boxes."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
    sock.settimeout(timeout)
    for target in SSDP_TARGETS:
        msg = (
            "M-SEARCH * HTTP/1.1\r\n"
            f"HOST: {SSDP_ADDR[0]}:{SSDP_ADDR[1]}\r\n"
            'MAN: "ssdp:discover"\r\nMX: 2\r\n'
            f"ST: {target}\r\n\r\n"
        )
        sock.sendto(msg.encode(), SSDP_ADDR)
    hosts: list[str] = []
    try:
        while True:
            data, (addr, _) = sock.recvfrom(4096)
            # Some devices answer every M-SEARCH, so keep only Sky Q responders.
            headers = data.decode("latin-1").lower()
            if "skycontrol" in headers and addr not in hosts:
                hosts.append(addr)
    except socket.timeout:
        pass
    finally:
        sock.close()
    return hosts


class Probe:
    """Probe a single Sky Q box and accumulate results and fixtures."""

    def __init__(self, host: str, args: argparse.Namespace, session: aiohttp.ClientSession) -> None:
        self.host = host
        self.args = args
        self.session = session
        self.results: dict[str, Any] = {
            "host": host, "started": now_iso(), "probe_version": VERSION,
            "remote_ports": [], "rest": [], "explore": [], "upnp": {"descriptions": [], "devices": [], "scpd": {}},
            "soap": [], "browse": [], "websockets": [], "inventory": {}, "writes": [], "key_sweep": [],
            "listen": [], "hypotheses": [],
        }
        self.fixtures: dict[str, Any] = {}
        self.tags: dict[str, dict] = {}
        self.services: list[dict] = []  # UPnP services with parsed SCPD
        self.av_service: dict | None = None
        self.device_info: dict = {}
        self.current_sid: int | None = None
        self.pvr: dict = {}
        self.channel_list: list[dict] = []

    # ----------------------------------------------------------------- transport helpers

    async def rest(self, method: str, path: str, *, name: str | None = None, section: str = "rest",
                   tag: str | None = None, record: bool = True, timeout: float = 8) -> tuple[int | None, Any]:
        url = f"http://{self.host}:{REST_PORT}/as/{path}".rstrip("/")
        entry: dict[str, Any] = {"method": method, "path": "/as/" + path}
        text = None
        try:
            async with self.session.request(method, url, timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
                text = (await resp.read()).decode("utf-8", "replace")
                entry["status"] = resp.status
                entry["content_type"] = resp.headers.get("Content-Type")
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            entry["status"] = None
            entry["error"] = f"{type(err).__name__}: {err}"
        parsed: Any = None
        if text:
            try:
                parsed = json.loads(text)
            except ValueError:
                parsed = None
        entry["ok"] = looks_ok(entry.get("status"), parsed)
        entry["summary"] = entry.get("error") or summarise(parsed, text)
        if name and text is not None:
            entry["fixture"] = self.add_fixture(f"rest/{name}", parsed if parsed is not None else text)
        if record:
            self.results[section].append(entry)
            self.log(f"{method:6} {entry['path'][:70]:70} {entry.get('status')!s:4} {'ok' if entry['ok'] else '--'}")
        if tag:
            self.tags[tag] = entry
        return entry.get("status"), parsed

    async def soap(self, service: dict, action: str, args: dict[str, str], *, record: bool = True,
                   tag: str | None = None) -> dict:
        stype = service["serviceType"]
        body = "".join(f"<{k}>{escape(v)}</{k}>" for k, v in args.items())
        envelope = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
            's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
            f'<s:Body><u:{action} xmlns:u="{stype}">{body}</u:{action}></s:Body></s:Envelope>'
        )
        headers = {
            "Content-Type": 'text/xml; charset="utf-8"',
            "SOAPACTION": f'"{stype}#{action}"',
            "User-Agent": UPNP_USER_AGENT,
        }
        entry: dict[str, Any] = {"service": service["serviceId"], "action": action, "args": args}
        try:
            async with self.session.post(self.upnp_url(service["controlURL"]), data=envelope, headers=headers,
                                         timeout=aiohttp.ClientTimeout(total=5)) as resp:
                text = (await resp.read()).decode("utf-8", "replace")
                entry["status"] = resp.status
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            entry.update(status=None, ok=False, error=f"{type(err).__name__}: {err}")
            text = ""
        if text:
            try:
                root = strip_ns(ET.fromstring(text))
                response = root.find(f".//{action}Response")
                if response is not None:
                    entry["response"] = {c.tag: c.text for c in response}
                elif root.find(".//Fault") is None:
                    entry["response"] = {}
                else:
                    entry["fault"] = {k: text_of(root.find(".//Fault") if k == "faultstring" else root.find(".//UPnPError"), k)
                                      for k in ("faultstring", "errorCode", "errorDescription")}
            except ET.ParseError:
                entry["raw"] = text[:500]
        entry["ok"] = entry.get("status") == 200 and "response" in entry
        if record:
            self.results["soap"].append(entry)
            short = entry.get("fault") or entry.get("error") or ""
            self.log(f"SOAP   {service['serviceId'].split(':')[-1]}#{action:28} {entry.get('status')!s:4} "
                     f"{'ok' if entry['ok'] else '--'} {short if short else ''}")
        if tag:
            self.tags[tag] = entry
        return entry

    def upnp_url(self, path: str) -> str:
        if path.startswith("http"):
            return path
        return f"http://{self.host}:{UPNP_PORT}{path if path.startswith('/') else '/' + path}"

    async def send_key(self, code: int, port: int) -> str | None:
        """Send one key with the Sky handshake. Returns an error string or None."""
        packet = bytearray([4, 1, 0, 0, 0, 0, 224 + code // 16, code % 16])
        try:
            reader, writer = await asyncio.wait_for(asyncio.open_connection(self.host, port), 3)
        except (OSError, asyncio.TimeoutError) as err:
            return f"connect failed: {err}"
        try:
            strlen = 12
            async with asyncio.timeout(5):
                while True:
                    data = await reader.read(1024)
                    if not data:
                        return "connection closed during handshake"
                    if len(data) < 24:
                        writer.write(data[:strlen])
                        strlen = 1
                    else:
                        writer.write(bytes(packet))
                        packet[1] = 0
                        writer.write(bytes(packet))
                        await writer.drain()
                        return None
                    await writer.drain()
        except (OSError, TimeoutError) as err:
            return f"handshake failed: {err!r}"
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass

    def add_fixture(self, name: str, data: Any) -> str:
        ext = ".json" if isinstance(data, (dict, list)) else (".xml" if str(data).lstrip().startswith("<") else ".txt")
        rel = name + ext
        self.fixtures[rel] = data
        return rel

    def log(self, msg: str) -> None:
        if not self.args.quiet:
            print(f"[{self.host}] {msg}")

    # ----------------------------------------------------------------- probe stages

    async def run(self) -> None:
        await self.probe_remote_ports()
        await self.probe_core_rest()
        await self.probe_upnp()
        await self.probe_derived_rest()
        await self.probe_explore()
        await self.rest("POST", "system/action/ping", name="system_action_ping", tag="ping")
        await self.probe_websockets()
        if self.args.listen:
            await self.listen(self.args.listen)
        if self.args.key_sweep:
            await self.key_sweep(parse_codes(self.args.codes))
        if self.args.key_batch:
            await self.key_batch(parse_codes(self.args.key_batch), self.args.gap)
        if self.args.writes:
            await self.writes()
        self.build_inventory()
        self.evaluate_hypotheses()
        self.results["finished"] = now_iso()

    async def probe_remote_ports(self) -> None:
        self.log("checking remote-control ports (connect + read banner only, no key sent)")
        for port in REMOTE_PORTS:
            entry: dict[str, Any] = {"port": port, "open": False}
            try:
                reader, writer = await asyncio.wait_for(asyncio.open_connection(self.host, port), 3)
                entry["open"] = True
                try:
                    data = await asyncio.wait_for(reader.read(64), 3)
                    entry["banner"] = data.decode("latin-1").strip()
                    entry["banner_hex"] = data.hex()
                    # macOS Screen Sharing and other VNC servers also listen on 5900.
                    entry["sky"] = entry["banner"].startswith("SKY")
                except asyncio.TimeoutError:
                    entry["banner"] = None
                writer.close()
                try:
                    await writer.wait_closed()
                except OSError:
                    pass
            except (OSError, asyncio.TimeoutError) as err:
                entry["error"] = f"{type(err).__name__}: {err}"
            self.results["remote_ports"].append(entry)
            self.log(f"TCP    {port}: {'open, banner=' + repr(entry.get('banner')) if entry['open'] else 'closed'}")

    @property
    def remote_port(self) -> int | None:
        ports = self.results["remote_ports"]
        return next((p["port"] for p in ports if p.get("sky")), None)

    async def probe_core_rest(self) -> None:
        _, info = await self.rest("GET", "system/deviceinformation", name="system_deviceinformation", tag="deviceinfo")
        self.device_info = info if isinstance(info, dict) else {}
        _, sysinfo = await self.rest("GET", "system/information", name="system_information", tag="sysinfo")
        if isinstance(sysinfo, dict):
            self.device_info = {**sysinfo, **self.device_info}
        await self.rest("GET", "system/time", name="system_time", tag="time")
        await self.rest("GET", "apps", name="apps", tag="apps")
        await self.rest("GET", "services", name="services_plain", tag="services_plain")
        await self.rest("GET", "services/favourites", name="services_favourites", tag="favourites")
        await self.rest("GET", "pvr/storage", name="pvr_storage", tag="storage")
        await self.rest("GET", "pin", name="pin", tag="pin")
        _, pvr = await self.rest("GET", "pvr?limit=1000&offset=0", name="pvr_list", tag="pvr_list", timeout=20)
        self.pvr = pvr if isinstance(pvr, dict) else {}

    async def probe_upnp(self) -> None:
        self.log(f"fetching UPnP descriptions 0..{self.args.max_descriptions - 1}")
        sem = asyncio.Semaphore(6)

        async def fetch(index: int) -> tuple[int, int | None, str]:
            url = f"http://{self.host}:{UPNP_PORT}/description{index}.xml"
            async with sem:
                try:
                    async with self.session.get(url, headers={"User-Agent": UPNP_USER_AGENT},
                                                timeout=aiohttp.ClientTimeout(total=3)) as resp:
                        return index, resp.status, (await resp.read()).decode("utf-8", "replace")
                except (aiohttp.ClientError, asyncio.TimeoutError):
                    return index, None, ""

        found = await asyncio.gather(*(fetch(i) for i in range(self.args.max_descriptions)))
        seen_scpd: dict[str, dict] = {}
        for index, status, text in found:
            if status != 200 or not text:
                continue
            self.results["upnp"]["descriptions"].append(index)
            self.add_fixture(f"upnp/description{index}", text)
            try:
                root = strip_ns(ET.fromstring(text))
            except ET.ParseError:
                continue
            for dev in root.iter("device"):
                device = {
                    "description": index,
                    **{k: text_of(dev, k) for k in ("deviceType", "friendlyName", "manufacturer",
                                                    "modelName", "modelNumber", "modelDescription")},
                    "services": [],
                }
                service_list = dev.find("serviceList")
                for svc in service_list.findall("service") if service_list is not None else []:
                    service = {k: text_of(svc, k) for k in ("serviceType", "serviceId", "controlURL",
                                                            "eventSubURL", "SCPDURL")}
                    device["services"].append(service)
                    scpd_url = service.get("SCPDURL")
                    if scpd_url and scpd_url not in seen_scpd:
                        seen_scpd[scpd_url] = await self.fetch_scpd(service)
                    service["scpd"] = seen_scpd.get(scpd_url or "", {})
                    if service.get("controlURL") and service.get("serviceType"):
                        self.services.append(service)
                self.results["upnp"]["devices"].append(
                    {**device, "services": [{k: v for k, v in s.items() if k != "scpd"} for s in device["services"]]})
                self.log(f"UPnP   description{index}: {device['deviceType']} with "
                         f"{len(device['services'])} service(s)")
        await self.probe_soap_reads()

    async def fetch_scpd(self, service: dict) -> dict:
        url = self.upnp_url(service["SCPDURL"])
        try:
            async with self.session.get(url, headers={"User-Agent": UPNP_USER_AGENT},
                                        timeout=aiohttp.ClientTimeout(total=4)) as resp:
                text = (await resp.read()).decode("utf-8", "replace")
                if resp.status != 200:
                    return {"error": f"HTTP {resp.status}"}
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            return {"error": str(err)}
        self.add_fixture(f"upnp/scpd_{slug(service.get('serviceId') or service['SCPDURL'])}", text)
        try:
            root = strip_ns(ET.fromstring(text))
        except ET.ParseError:
            return {"error": "unparseable"}
        variables = {}
        for var in root.iter("stateVariable"):
            allowed = [a.text for a in var.iter("allowedValue")]
            variables[text_of(var, "name")] = {
                "dataType": text_of(var, "dataType"), "sendEvents": var.get("sendEvents"),
                **({"allowed": allowed} if allowed else {}),
            }
        actions = {}
        for act in root.iter("action"):
            args = []
            for arg in act.iter("argument"):
                args.append({"name": text_of(arg, "name"), "direction": text_of(arg, "direction"),
                             "var": text_of(arg, "relatedStateVariable")})
            actions[text_of(act, "name")] = args
        scpd = {"actions": actions, "stateVariables": variables}
        self.results["upnp"]["scpd"][service.get("serviceId") or service["SCPDURL"]] = scpd
        return scpd

    @staticmethod
    def default_arg(name: str) -> str | None:
        return {
            "instanceid": "0", "channel": "Master", "objectid": "0", "containerid": "0",
            "browseflag": "BrowseDirectChildren", "filter": "*", "startingindex": "0",
            "requestedcount": "25", "sortcriteria": "", "searchcriteria": "*",
        }.get(name.lower())

    def fill_args(self, args: list[dict]) -> dict[str, str] | None:
        filled = {}
        for arg in args:
            if arg["direction"] != "in":
                continue
            value = self.default_arg(arg["name"] or "")
            if value is None:
                return None
            filled[arg["name"]] = value
        return filled

    async def probe_soap_reads(self) -> None:
        for service in self.services:
            actions = service.get("scpd", {}).get("actions", {})
            for action, args in actions.items():
                if not (action.startswith("Get") or action in ("Browse", "Search")):
                    continue
                filled = self.fill_args(args)
                if filled is None:
                    self.results["soap"].append({"service": service["serviceId"], "action": action,
                                                 "skipped": "needs arguments we cannot safely invent"})
                    continue
                # The gateway's SkyServe device repeats SkyPlay with a control URL that rejects
                # every action, so keep the first successful result per action.
                tag = f"soap:{action}" if not (self.tags.get(f"soap:{action}") or {}).get("ok") else None
                entry = await self.soap(service, action, filled, tag=tag)
                if entry["ok"]:
                    name = f"soap/{service['serviceId'].split(':')[-1]}_{action}"
                    while name + ".json" in self.fixtures:
                        name += "_alt"
                    self.add_fixture(name, entry["response"])
                    if action == "GetTransportInfo" and self.av_service is None:
                        self.av_service = service
                if action == "GetMediaInfo" and entry["ok"]:
                    uri = entry["response"].get("CurrentURI") or ""
                    if uri.startswith("xsi://"):
                        try:
                            self.current_sid = int(uri[6:], 16)
                        except ValueError:
                            pass
                if action == "Browse" and entry["ok"]:
                    await self.browse_children(service, entry["response"].get("Result") or "")

    async def browse_children(self, service: dict, didl: str) -> None:
        """Browse one level into each container returned at the root."""
        try:
            root = strip_ns(ET.fromstring(didl))
        except ET.ParseError:
            return
        containers = [(c.get("id"), text_of(c, "title")) for c in root.iter("container")]
        items = len(list(root.iter("item")))
        self.results["browse"].append({"object": "0", "containers": containers, "items": items})
        for cid, title in containers[:8]:
            entry = await self.soap(service, "Browse", {
                "ObjectID": cid, "BrowseFlag": "BrowseDirectChildren", "Filter": "*",
                "StartingIndex": "0", "RequestedCount": "25", "SortCriteria": "",
            })
            if entry["ok"]:
                self.add_fixture(f"soap/browse_{slug(cid or 'x')}", entry["response"])
                try:
                    sub = strip_ns(ET.fromstring(entry["response"].get("Result") or "<x/>"))
                    self.results["browse"].append({"object": cid, "title": title,
                                                   "containers": len(list(sub.iter("container"))),
                                                   "items": len(list(sub.iter("item"))),
                                                   "total": entry["response"].get("TotalMatches")})
                except ET.ParseError:
                    pass

    async def probe_derived_rest(self) -> None:
        bouquet, sub = self.device_info.get("bouquet"), self.device_info.get("subbouquet")
        services: list[dict] = []
        if bouquet is not None and sub is not None:
            _, data = await self.rest("GET", f"services/{bouquet}/{sub}", name="services_bouquet", tag="services")
            if isinstance(data, dict):
                services = data.get("services") or []
        self.channel_list = services
        sids = []
        if self.current_sid:
            sids.append(str(self.current_sid))
        if services and str(services[0].get("sid")) not in sids:
            sids.append(str(services[0].get("sid")))
        for i, sid in enumerate(sids):
            await self.rest("GET", f"services/details/{sid}", name=f"services_details_{i}",
                            tag="service_details" if i == 0 else None)

        items = self.pvr.get("pvrItems") or []
        by_status: dict[str, dict] = {}
        for item in items:
            by_status.setdefault(str(item.get("status")), item)
        for status, item in by_status.items():
            await self.rest("GET", f"pvr/details/{item.get('pvrid')}", name=f"pvr_details_{slug(status)}",
                            tag="pvr_details")
        doc, ver = self.pvr.get("documentId"), self.pvr.get("version")
        if doc is not None and ver is not None:
            await self.rest("GET", f"pvr/changes/{doc}/{ver}", name="pvr_changes_current", tag="pvr_changes")
            if isinstance(ver, int) and ver > 0:
                await self.rest("GET", f"pvr/changes/{doc}/{ver - 1}", name="pvr_changes_previous",
                                tag="pvr_changes_prev")

        date = datetime.now(timezone.utc).strftime("%Y%m%d")
        sid = sids[0] if sids else None
        eid = None
        if sid:
            _, sched = await self.rest("GET", f"schedule/{date}/{sid}", name="schedule_box", tag="schedule_box")
            eid = first_eid(sched)
            if not self.args.no_cloud:
                cloud = await self.cloud_schedule(date, sid)
                eid = eid or first_eid(cloud)
        if not eid:
            eid = next((i.get("oeid") for i in items if i.get("oeid")), None)
        if eid:
            await self.rest("GET", f"schedule/event/{eid}", name="schedule_event", tag="schedule_event")

    async def cloud_schedule(self, date: str, sid: str) -> Any:
        url = CLOUD_SCHEDULE_URL.format(date=date, sid=sid)
        entry: dict[str, Any] = {"method": "GET", "path": url}
        try:
            async with self.session.get(url, headers=CLOUD_HEADERS, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                entry["status"] = resp.status
                data = json.loads((await resp.read()).decode("utf-8", "replace"))
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as err:
            entry.update(status=entry.get("status"), error=str(err))
            data = None
        entry["ok"] = looks_ok(entry.get("status"), data)
        entry["summary"] = entry.get("error") or summarise(data, None)
        if data is not None:
            entry["fixture"] = self.add_fixture("cloud/schedule", data)
        self.results["rest"].append(entry)
        self.tags["schedule_cloud"] = entry
        self.log(f"GET    {url[:70]:70} {entry.get('status')!s:4} {'ok' if entry['ok'] else '--'}")
        return data

    async def probe_explore(self) -> None:
        self.log("exploring undocumented GET paths (404s are expected)")
        for path in EXPLORE_GETS:
            await self.rest("GET", path, name=f"explore_{slug(path)}", section="explore", timeout=5)

    async def probe_websockets(self) -> None:
        secs = self.args.ws_seconds
        self.log(f"listening on {len(WS_CANDIDATES)} websocket candidates for {secs}s each (in parallel)")
        entries = await self.ws_many(WS_CANDIDATES, secs)
        self.results["websockets"].extend(entries)
        for e in entries:
            self.log(f"WS     /as/{e['path']:28} {'connected' if e['connected'] else 'no':9} "
                     f"msgs={e['messages']} {e.get('error', '')}")

    async def ws_many(self, paths: list[str], seconds: float, **kwargs: Any) -> list[dict]:
        """Listen on several websockets; never let one stuck socket stall the probe."""
        entries = [{"path": p, "connected": False, "messages": 0} for p in paths]
        tasks = [asyncio.create_task(self.ws_listen(e["path"], seconds, entry=e, **kwargs)) for e in entries]
        await asyncio.wait(tasks, timeout=seconds + 25)
        for entry, task in zip(entries, tasks):
            if not task.done():
                # The gateway sometimes stalls a socket after its first message; keep what arrived.
                task.cancel()
                entry["error"] = f"stalled after {entry['messages']} message(s); cancelled"
                self.finish_ws_entry(entry, kwargs.get("fixture_prefix", "ws"))
            elif not task.cancelled() and task.exception() is not None:
                entry["error"] = repr(task.exception())
            entry.pop("_messages", None)
        return entries

    def finish_ws_entry(self, entry: dict, fixture_prefix: str) -> None:
        messages = entry.get("_messages") or []
        if messages and "fixture" not in entry:
            entry["first_keys"] = summarise(messages[0]["data"], None)
            entry["fixture"] = self.add_fixture(f"{fixture_prefix}/{slug(entry['path'])}", messages)

    async def ws_listen(self, path: str, seconds: float, on_message=None, fixture_prefix: str = "ws",
                        entry: dict | None = None) -> dict:
        url = f"ws://{self.host}:{REST_PORT}/as/{path}"
        if entry is None:
            entry = {"path": path, "connected": False, "messages": 0}
        messages: list[dict] = entry.setdefault("_messages", [])
        loop = asyncio.get_running_loop()
        try:
            ws = await asyncio.wait_for(self.session.ws_connect(url), 6)
        except aiohttp.WSServerHandshakeError as err:
            entry["error"] = f"handshake HTTP {err.status}"
            return entry
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            entry["error"] = type(err).__name__
            return entry
        entry["connected"] = True
        start = loop.time()
        try:
            while (remaining := start + seconds - loop.time()) > 0:
                try:
                    msg = await ws.receive(timeout=remaining)
                except asyncio.TimeoutError:
                    break
                if msg.type in (aiohttp.WSMsgType.TEXT, aiohttp.WSMsgType.BINARY):
                    raw = msg.data if isinstance(msg.data, str) else msg.data.decode("utf-8", "replace")
                    try:
                        data: Any = json.loads(raw)
                    except ValueError:
                        data = raw
                    entry["messages"] += 1
                    changed = diff_keys(messages[-1]["data"], data) if messages else None
                    if len(messages) < self.args.max_ws_messages:
                        messages.append({"t": round(loop.time() - start, 1), "data": data, "changed": changed})
                    if on_message:
                        on_message(path, data, changed)
                elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                    entry["closed_by_box"] = True
                    break
        finally:
            # aiohttp's close() can block sending the close frame if the box stops reading.
            try:
                await asyncio.wait_for(ws.close(), 3)
            except (asyncio.TimeoutError, aiohttp.ClientError, OSError):
                pass
        self.finish_ws_entry(entry, fixture_prefix)
        return entry

    # ----------------------------------------------------------------- opt-in modes

    async def snapshot(self) -> dict:
        snap: dict[str, Any] = {}
        if self.av_service:
            ti = await self.soap(self.av_service, "GetTransportInfo", {"InstanceID": "0"}, record=False)
            mi = await self.soap(self.av_service, "GetMediaInfo", {"InstanceID": "0"}, record=False)
            snap["transport"] = (ti.get("response") or {}).get("CurrentTransportState")
            snap["speed"] = (ti.get("response") or {}).get("CurrentSpeed")
            snap["uri"] = (mi.get("response") or {}).get("CurrentURI")
            if "GetPositionInfo" in self.av_service.get("scpd", {}).get("actions", {}):
                pi = await self.soap(self.av_service, "GetPositionInfo", {"InstanceID": "0"}, record=False)
                snap["position"] = (pi.get("response") or {}).get("RelTime")
        _, info = await self.rest("GET", "system/information", record=False, timeout=4)
        if isinstance(info, dict):
            snap["activeStandby"] = info.get("activeStandby")
        try:
            ws = await asyncio.wait_for(self.session.ws_connect(f"ws://{self.host}:{REST_PORT}/as/apps/status"), 4)
            msg = await ws.receive(timeout=3)
            await ws.close()
            apps = json.loads(msg.data).get("apps", [])
            snap["visible_app"] = next((a.get("appId") for a in apps if a.get("status") == "VISIBLE"), None)
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, TypeError, AttributeError):
            pass
        return snap

    async def listen(self, seconds: int) -> None:
        print(f"\n=== LISTEN MODE for {seconds}s on {self.host} ===\n"
              "Use the real remote now. Suggested: change channel, pause/resume live TV, play a recording,\n"
              "seek in it, start a recording, open an app, go to standby and wake it again.\n")
        log = self.results["listen"]
        loop = asyncio.get_running_loop()
        start = loop.time()

        def on_ws(path: str, data: Any, changed: Any) -> None:
            item = {"t": round(loop.time() - start, 1), "source": f"ws:{path}", "changed": changed}
            log.append({**item, "data": data})
            print(f"  +{item['t']:>6}s  ws {path:20} changed={changed}")

        working = [e["path"] for e in self.results["websockets"] if e["messages"]] or ["system/status", "apps/status"]
        ws_task = asyncio.create_task(self.ws_many(working, seconds, on_message=on_ws, fixture_prefix="listen_ws"))
        previous: dict = {}
        while loop.time() - start < seconds:
            snap = await self.snapshot()
            changed = {k: (previous.get(k), v) for k, v in snap.items() if previous.get(k) != v}
            if previous and changed:
                t = round(loop.time() - start, 1)
                log.append({"t": t, "source": "poll", "changed": changed})
                print(f"  +{t:>6}s  poll {changed}")
            previous = snap
            await asyncio.sleep(2)
        await ws_task
        self.add_fixture("listen/events", log)

    async def key_sweep(self, codes: list[int]) -> None:
        port = self.remote_port
        if port is None:
            print("No port answered with a SKY banner; skipping key sweep.")
            return
        known = set(KNOWN_KEYS.values())
        codes = [c for c in codes if c not in known or self.args.include_known]
        print(f"\n=== KEY SWEEP on {self.host}:{port} — {len(codes)} codes ===\n"
              "Watch the TV. For each code: Enter = send, s = skip, q = quit.\n"
              "Afterwards describe what happened (blank = nothing visible). Have the real remote handy\n"
              "to back out of any menu. Start from live TV with no menus open.\n")
        for code in codes:
            choice = (await asyncio.to_thread(input, f"code {code:3}: send? [Enter/s/q] ")).strip().lower()
            if choice == "q":
                break
            if choice == "s":
                continue
            before = await self.snapshot()
            err = await self.send_key(code, port)
            await asyncio.sleep(1.5)
            after = await self.snapshot()
            effect = "" if err else (await asyncio.to_thread(input, "        effect? ")).strip()
            changed = {k: (before.get(k), v) for k, v in after.items() if before.get(k) != v}
            self.results["key_sweep"].append({"code": code, "error": err, "effect": effect, "state_change": changed})
            if changed:
                print(f"        state change detected: {changed}")

    async def key_batch(self, codes: list[int], gap: float) -> None:
        """Non-interactive sweep: send each code, log state changes, then press Back twice."""
        port = self.remote_port
        if port is None:
            print("No port answered with a SKY banner; skipping key batch.")
            return
        loop = asyncio.get_running_loop()
        start = loop.time()
        print(f"\n=== KEY BATCH on {self.host}:{port}: {codes} every {gap}s ===")
        for code in codes:
            before = await self.snapshot()
            sent_at = round(loop.time() - start, 1)
            err = await self.send_key(code, port)
            await asyncio.sleep(gap)
            after = await self.snapshot()
            changed = {k: (before.get(k), v) for k, v in after.items() if before.get(k) != v}
            self.results["key_sweep"].append({"code": code, "t": sent_at, "error": err, "effect": "",
                                              "state_change": changed})
            print(f"  +{sent_at:>5}s code {code:3} {'ERR ' + err if err else ''} {changed or ''}")
            for _ in range(2):
                await self.send_key(KNOWN_KEYS["backup"], port)
                await asyncio.sleep(0.6)

    async def writes(self) -> None:
        pvrid = self.args.pvrid
        print(f"\n=== WRITE TESTS on {self.host} ===")
        if not self.args.yes:
            print("These calls change box state. Each toggle is reversed straight after; erase is never called.")
            if pvrid:
                print(f"Recording under test: {pvrid}")
            answer = await asyncio.to_thread(input, "Type 'yes' to continue: ")
            if answer.strip().lower() != "yes":
                print("Skipped write tests.")
                return

        await self.soap_transport_tests()
        if pvrid:
            await self.pvr_write_tests(pvrid)
        if self.args.book_eid:
            status, body = await self.rest("POST", f"pvr/action/bookrecording?eid={self.args.book_eid}",
                                           name="write_bookrecording", section="writes", tag="write:book")
            print(f"Booked eid {self.args.book_eid}: HTTP {status}, body={body!r}")
            await self.booked_item_tests(self.args.book_eid)
        if self.args.app_launch:
            await self.app_launch_tests(self.args.app_launch)

    async def soap_transport_tests(self) -> None:
        svc = self.av_service
        if not svc:
            return
        actions = svc.get("scpd", {}).get("actions", {})
        before = await self.snapshot()
        record = self.results["writes"]
        if "Pause" in actions and "Play" in actions:
            pause = await self.soap(svc, "Pause", {"InstanceID": "0"}, tag="soap_write:Pause")
            await asyncio.sleep(2)
            mid = await self.snapshot()
            play = await self.soap(svc, "Play", {"InstanceID": "0", "Speed": "1"}, tag="soap_write:Play")
            await asyncio.sleep(2)
            after = await self.snapshot()
            record.append({"test": "soap_pause_play", "pause": pause, "play": play,
                           "states": [before.get("transport"), mid.get("transport"), after.get("transport")]})
        if "SetAVTransportURI" in actions:
            target = (f"xsi://{self.args.tune_sid:X}" if self.args.tune_sid else before.get("uri"))
            if target:
                tune = await self.soap(svc, "SetAVTransportURI", {
                    "InstanceID": "0", "CurrentURI": target, "CurrentURIMetaData": "NOT_IMPLEMENTED"},
                    tag="soap_write:SetAVTransportURI")
                await asyncio.sleep(4)
                after = await self.snapshot()
                record.append({"test": "soap_set_uri", "target": target, "result": tune,
                               "uri_before": before.get("uri"), "uri_after": after.get("uri"),
                               "changed_channel": before.get("uri") != after.get("uri")})

    async def pvr_details(self, pvrid: str) -> dict:
        _, data = await self.rest("GET", f"pvr/details/{pvrid}", record=False)
        return (data or {}).get("details", {}) if isinstance(data, dict) else {}

    async def toggle(self, pvrid: str, on: str, off: str, field: str) -> None:
        before = await self.pvr_details(pvrid)
        orig = bool(before.get(field))
        first, second = (off, on) if orig else (on, off)
        s1, _ = await self.rest("POST", f"pvr/action/{first}?pvrid={pvrid}", section="writes")
        mid = await self.pvr_details(pvrid)
        s2, _ = await self.rest("POST", f"pvr/action/{second}?pvrid={pvrid}", section="writes")
        after = await self.pvr_details(pvrid)
        result = {"test": f"{on}/{off}", "field": field, "http": [s1, s2],
                  "values": [before.get(field), mid.get(field), after.get(field)],
                  # Absent flags mean False on real boxes, so compare truthiness.
                  "works": bool(mid.get(field)) != orig, "restored": bool(after.get(field)) == orig}
        self.results["writes"].append(result)
        self.tags[f"write:{on}"] = result
        print(f"  {on}/{off}: {result['values']} works={result['works']} restored={result['restored']}")

    async def pvr_write_tests(self, pvrid: str) -> None:
        details = await self.pvr_details(pvrid)
        if not details:
            print(f"Recording {pvrid} not found; skipping PVR write tests.")
            return
        print(f"Testing on '{details.get('t')}' ({details.get('status')})")
        await self.toggle(pvrid, "keep", "unkeep", "keep")
        await self.toggle(pvrid, "lock", "unlock", "lock")
        if details.get("status") == "SCHEDULED":
            await self.toggle(pvrid, "serieslink", "seriesunlink", "link")

        # Sky Q boxes accept forward moves only: setting an earlier position returned 500 and then
        # hung, so this test nudges the resume point 5 s forward and cannot restore it.
        for variant in ("pvr/action/setlastplayedposition", "pvr/setlastplayedposition"):
            before = (await self.pvr_details(pvrid)).get("lp") or 0
            status, _ = await self.rest("POST", f"{variant}?pos={before + 5}&pvrid={pvrid}", section="writes",
                                        timeout=6)
            new = (await self.pvr_details(pvrid)).get("lp")
            works = status is not None and 200 <= status < 300 and new == before + 5
            result = {"test": "setlastplayedposition", "path": variant, "http": status, "lp": [before, new],
                      "works": works, "note": "forward-only; not restored"}
            self.results["writes"].append(result)
            self.tags[f"write:lastpos:{variant}"] = result
            print(f"  {variant}: lp {before} -> {new} works={works} (forward-only, not restored)")
            if works:
                break

        if self.args.allow_delete:
            s1, _ = await self.rest("POST", f"pvr/action/delete?pvrid={pvrid}", section="writes")
            mid = await self.pvr_details(pvrid)
            s2, _ = await self.rest("POST", f"pvr/action/undelete?pvrid={pvrid}", section="writes")
            after = await self.pvr_details(pvrid)
            result = {"test": "delete/undelete", "http": [s1, s2],
                      "status": [details.get("status"), mid.get("status"), after.get("status")],
                      "restored": after.get("status") == details.get("status")}
            self.results["writes"].append(result)
            self.tags["write:delete"] = result
            print(f"  delete/undelete: {result['status']} restored={result['restored']}")

    async def booked_item_tests(self, eid: str) -> None:
        """Series-link the new booking, then (with --allow-delete) cancel it so nothing is left behind."""
        await asyncio.sleep(2)
        _, data = await self.rest("GET", "pvr?limit=1000&offset=0", record=False, timeout=20)
        items = (data or {}).get("pvrItems", []) if isinstance(data, dict) else []
        booked = next((i for i in items if i.get("oeid") == eid and i.get("status") == "SCHEDULED"), None)
        self.results["writes"].append({"test": "find_booking", "eid": eid, "found": bool(booked),
                                       "item": booked})
        if not booked:
            print(f"  booking for {eid} not found as SCHEDULED; remove it with the remote if it exists.")
            return
        pvrid = booked["pvrid"]
        print(f"  booking is {pvrid} ({booked.get('t')})")
        await self.toggle(pvrid, "serieslink", "seriesunlink", "link")
        if not self.args.allow_delete:
            print(f"  leaving {pvrid} booked; remove it with the remote if you don't want it.")
            return
        s1, _ = await self.rest("POST", f"pvr/action/delete?pvrid={pvrid}", section="writes")
        remaining: list[dict] = []
        for _ in range(6):  # the box applies the cancel a few seconds later
            await asyncio.sleep(2)
            _, after = await self.rest("GET", "pvr?limit=1000&offset=0", record=False, timeout=20)
            remaining = [i for i in (after or {}).get("pvrItems", []) if i.get("pvrid") == pvrid]
            if not remaining or remaining[0].get("status") != "SCHEDULED":
                break
        result = {"test": "cancel_booking", "pvrid": pvrid, "http": s1,
                  "status_after": remaining[0].get("status") if remaining else "gone",
                  "works": not remaining or remaining[0].get("status") != "SCHEDULED"}
        self.results["writes"].append(result)
        self.tags["write:cancel_booking"] = result
        print(f"  cancel booking via delete: status after = {result['status_after']} works={result['works']}")

    async def app_launch_tests(self, app: str) -> None:
        for template in APP_LAUNCH_CANDIDATES:
            before = (await self.snapshot()).get("visible_app")
            status, _ = await self.rest("POST", template.format(app=app), section="writes")
            accepted = status is not None and 200 <= status < 300
            after = before
            for _ in range(5 if accepted else 0):  # apps can take several seconds to come up
                await asyncio.sleep(2)
                after = (await self.snapshot()).get("visible_app")
                if after == app:
                    break
            launched = accepted and after == app
            result = {"test": "app_launch", "path": template, "http": status, "visible": [before, after],
                      "works": launched}
            self.results["writes"].append(result)
            if accepted:
                self.tags["write:app_launch"] = result
                print(f"  app launch {'works' if launched else 'accepted (app not seen)'} via {template}")
                return
        self.tags["write:app_launch"] = {"works": False}

    # ----------------------------------------------------------------- analysis

    def build_inventory(self) -> None:
        inv = self.results["inventory"]
        items = self.pvr.get("pvrItems") or []
        if items:
            inv["pvr"] = {
                "total": len(items),
                "totalPvrItems": self.pvr.get("totalPvrItems"),
                "status": dict(Counter(str(i.get("status")) for i in items)),
                "src": dict(Counter(str(i.get("src")) for i in items)),
                "type": dict(Counter(str(i.get("type")) for i in items)),
                "failure_reasons": dict(Counter(str(i.get("fr")) for i in items if "fr" in i)),
                "field_counts": dict(Counter(k for i in items for k in i).most_common()),
            }
        channels = self.channel_list
        if channels:
            inv["services"] = {
                "total": len(channels),
                "sf": dict(Counter(str(c.get("sf")) for c in channels)),
                "servicetype": dict(Counter(str(c.get("servicetype")) for c in channels)),
                "field_counts": dict(Counter(k for c in channels for k in c).most_common()),
            }
        for name in ("deviceinfo", "sysinfo", "time"):
            entry = self.tags.get(name)
            if entry and entry.get("fixture"):
                data = self.fixtures.get(entry["fixture"])
                if isinstance(data, dict):
                    inv[f"{name}_fields"] = sorted(data)
        status_ws = next((e for e in self.results["websockets"] if e["path"] == "system/status"), None)
        if status_ws and status_ws.get("fixture"):
            first = self.fixtures[status_ws["fixture"]][0]["data"]
            if isinstance(first, dict):
                inv["system_status_sections"] = {k: (sorted(v) if isinstance(v, dict) else type(v).__name__)
                                                 for k, v in first.items()}

    def evaluate_hypotheses(self) -> None:
        def ok(tag: str) -> bool | None:
            entry = self.tags.get(tag)
            return None if entry is None else bool(entry.get("ok", entry.get("works")))

        actions = (self.av_service or {}).get("scpd", {}).get("actions", {})
        all_actions = {a for s in self.services for a in s.get("scpd", {}).get("actions", {})}
        ws_ok = [e["path"] for e in self.results["websockets"] if e["messages"]]
        extra_ws = [p for p in ws_ok if p not in ("system/status", "apps/status")]
        browse_items = sum(b.get("items") or 0 for b in self.results["browse"] if isinstance(b.get("items"), int))
        ports = {p["port"]: bool(p.get("sky")) for p in self.results["remote_ports"]}
        explore_hits = [e["path"] for e in self.results["explore"] if e["ok"]]
        sweep = self.results["key_sweep"]
        lastpos = {k.split(":", 2)[2]: v.get("works") for k, v in self.tags.items() if k.startswith("write:lastpos:")}

        rows = [
            ("Remote port 49160 answers SKY handshake", ports.get(49160), f"legacy 5900: {ports.get(5900)}"),
            ("REST system/deviceinformation", ok("deviceinfo"), ""),
            ("REST system/information", ok("sysinfo"), ""),
            ("REST pvr list", ok("pvr_list"), f"{len(self.pvr.get('pvrItems') or [])} items"),
            ("REST pvr/changes incremental diff", ok("pvr_changes"), ""),
            ("REST pin status", ok("pin"), ""),
            ("REST services/details/{sid}", ok("service_details"), ""),
            ("REST box EPG schedule/{date}/{sid}", ok("schedule_box"), f"cloud EPG ok: {ok('schedule_cloud')}"),
            ("REST box EPG schedule/event/{eid}", ok("schedule_event"), ""),
            ("REST system/action/ping", ok("ping"), ""),
            ("WS system/status", "system/status" in ws_ok, ""),
            ("WS apps/status", "apps/status" in ws_ok, ""),
            ("Extra push websockets (sent messages)", bool(extra_ws) if self.results["websockets"] else None,
             ", ".join(extra_ws)),
            ("SOAP GetTransportInfo", ok("soap:GetTransportInfo"), ""),
            ("SOAP GetMediaInfo", ok("soap:GetMediaInfo"), f"current sid: {self.current_sid}"),
            ("SOAP GetPositionInfo (playback position)", ok("soap:GetPositionInfo") if "GetPositionInfo" in actions
             else (False if self.services else None), "not in SCPD" if self.services and "GetPositionInfo" not in actions else ""),
            ("SOAP SetAVTransportURI in SCPD", ("SetAVTransportURI" in actions) if self.services else None, ""),
            ("SOAP SetAVTransportURI tunes", ok("soap_write:SetAVTransportURI"), "needs --writes"),
            ("SOAP Play/Pause/Seek/Stop in SCPD", bool({"Play", "Pause", "Seek", "Stop"} & all_actions)
             if self.services else None, ", ".join(sorted({"Play", "Pause", "Seek", "Stop", "Next", "Previous"} & all_actions))),
            ("SOAP Pause works", ok("soap_write:Pause"), "needs --writes"),
            ("SOAP Browse lists content", (browse_items > 0) if self.results["browse"] else
             (False if self.services and "Browse" not in all_actions else None), f"{browse_items} items"),
            ("PVR keep/unkeep", ok("write:keep"), "needs --writes --pvrid"),
            ("PVR lock/unlock", ok("write:lock"), "needs --writes --pvrid"),
            ("PVR serieslink/unlink", ok("write:serieslink"), "needs a SCHEDULED --pvrid"),
            ("PVR setlastplayedposition path", any(lastpos.values()) if lastpos else None,
             ", ".join(f"{k}={v}" for k, v in lastpos.items())),
            ("PVR delete/undelete", (self.tags.get("write:delete") or {}).get("restored"), "needs --allow-delete"),
            ("PVR bookrecording", ok("write:book"), "needs --book-eid"),
            ("PVR cancel booking via delete", ok("write:cancel_booking"), "needs --book-eid --allow-delete"),
            ("App launch endpoint", ok("write:app_launch"), "needs --app-launch"),
            ("Undocumented GET paths", bool(explore_hits), ", ".join(explore_hits)),
            ("Undocumented key codes", any(s.get("effect") or s.get("state_change") for s in sweep) if sweep else None,
             ", ".join(str(s["code"]) for s in sweep if s.get("effect") or s.get("state_change"))),
        ]
        self.results["hypotheses"] = [
            {"item": i, "result": r, "evidence": "" if r is not None and e.startswith("needs") else e}
            for i, r, e in rows
        ]


def first_eid(schedule: Any) -> str | None:
    if not isinstance(schedule, dict):
        return None
    for sched in schedule.get("schedule") or []:
        for event in sched.get("events") or []:
            if event.get("eid"):
                return event["eid"]
    return None


def diff_keys(old: Any, new: Any, prefix: str = "") -> list[str]:
    if isinstance(old, dict) and isinstance(new, dict):
        out: list[str] = []
        for key in set(old) | set(new):
            out.extend(diff_keys(old.get(key), new.get(key), f"{prefix}{key}."))
        return out
    return [] if old == new else [prefix.rstrip(".") or "<root>"]


# --------------------------------------------------------------------- output

def mark(value: Any) -> str:
    return {True: "✅", False: "❌", None: "⚪ not tested"}.get(value, str(value))


def render_report(probe: Probe) -> str:
    r = probe.results
    info = probe.device_info
    lines = [
        f"# Sky Q probe report: {probe.host}",
        "",
        f"Probe v{VERSION}, started {r['started']}, finished {r.get('finished')}.",
        "",
        "## Device",
        "",
        "| Field | Value |", "|---|---|",
    ]
    for key in ("hardwareName", "hardwareModel", "modelNumber", "deviceType", "ASVersion", "versionNumber",
                "manufacturer", "countryCode", "gateway", "hdrCapable", "uhdCapable", "pipCapable",
                "streamingCapable", "activeStandby", "wakeReason"):
        if key in info:
            lines.append(f"| {key} | {info[key]} |")

    lines += ["", "## Hypothesis checklist", "", "| Item | Result | Evidence |", "|---|---|---|"]
    lines += [f"| {h['item']} | {mark(h['result'])} | {h['evidence']} |" for h in r["hypotheses"]]

    lines += ["", "## Remote-control ports", "", "| Port | Open | Banner |", "|---|---|---|"]
    lines += [f"| {p['port']} | {mark(p['open'])} | `{p.get('banner')}` |" for p in r["remote_ports"]]

    for section, title in (("rest", "REST endpoints"), ("explore", "Exploratory GETs")):
        lines += ["", f"## {title}", "", "| Method | Path | HTTP | OK | Summary | Fixture |", "|---|---|---|---|---|---|"]
        for e in r[section]:
            summary = (e.get("summary") or "").replace("|", "/")[:100]
            lines.append(f"| {e['method']} | `{e['path']}` | {e.get('status')} | {mark(e['ok'])} | {summary} | "
                         f"{e.get('fixture', '')} |")

    lines += ["", "## UPnP", ""]
    for dev in r["upnp"]["devices"]:
        lines.append(f"- description{dev['description']}: **{dev['deviceType']}** ({dev.get('friendlyName')})")
        for s in dev["services"]:
            lines.append(f"  - `{s['serviceId']}` ({s['serviceType']}) control `{s['controlURL']}`")
    for sid, scpd in r["upnp"]["scpd"].items():
        lines += ["", f"### {sid}", ""]
        for action, args in (scpd.get("actions") or {}).items():
            ins = ", ".join(a["name"] for a in args if a["direction"] == "in")
            outs = ", ".join(a["name"] for a in args if a["direction"] == "out")
            lines.append(f"- `{action}({ins})` → {outs or '-'}")
    lines += ["", "### SOAP calls", "", "| Service | Action | HTTP | OK | Result |", "|---|---|---|---|---|"]
    for s in r["soap"]:
        result = s.get("skipped") or s.get("error") or json.dumps(s.get("response") or s.get("fault"))[:120]
        lines.append(f"| {s['service'].split(':')[-1]} | {s['action']} | {s.get('status', '')} | "
                     f"{mark(s.get('ok')) if 'skipped' not in s else 'skipped'} | {result.replace('|', '/')} |")

    lines += ["", "## Websockets", "", "| Path | Connected | Messages | First message |", "|---|---|---|---|"]
    lines += [f"| `/as/{w['path']}` | {mark(w['connected'])} | {w['messages']} | "
              f"{w.get('first_keys') or w.get('error', '')} |" for w in r["websockets"]]

    lines += ["", "## Field inventory", "", "```json", json.dumps(r["inventory"], indent=2), "```"]
    if r["writes"]:
        lines += ["", "## Write tests", "", "```json", json.dumps(r["writes"], indent=2, default=str)[:20000], "```"]
    if r["key_sweep"]:
        lines += ["", "## Key sweep", "", "| Code | Effect | State change |", "|---|---|---|"]
        lines += [f"| {k['code']} | {k['effect'] or k.get('error') or ''} | {k['state_change'] or ''} |"
                  for k in r["key_sweep"]]
    if r["listen"]:
        lines += ["", "## Listen log", "", "| t (s) | Source | Changed |", "|---|---|---|"]
        lines += [f"| {e['t']} | {e['source']} | {str(e['changed'])[:150]} |" for e in r["listen"]]
    return "\n".join(lines) + "\n"


def write_output(probe: Probe, out_root: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out = out_root / f"{slug(probe.host)}_{stamp}"
    out.mkdir(parents=True, exist_ok=True)
    secrets: set[str] = set()
    for data in probe.fixtures.values():
        collect_secrets(data, secrets)
    for data in probe.fixtures.values():
        if isinstance(data, str):
            secrets.update(m for m in re.findall(r"<serialNumber>([^<]{4,})</serialNumber>", data))
    secrets.discard(REDACTED)

    def scrub(text: str) -> str:
        for secret in sorted(secrets, key=len, reverse=True):
            text = text.replace(secret, REDACTED)
        text = re.sub(r"<serialNumber>[^<]*</serialNumber>", f"<serialNumber>{REDACTED}</serialNumber>", text)
        # UPnP descriptions also carry the NDS chip ID; the UDN uuid stays (it is in every control URL).
        return re.sub(r"(<(?!/)[^>]*X_NDS_ChipID[^>]*>)[^<]*", rf"\g<1>{REDACTED}", text)

    for rel, data in probe.fixtures.items():
        path = out / "fixtures" / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps(redact_obj(data), indent=2, default=str) if isinstance(data, (dict, list)) else str(data)
        path.write_text(scrub(text))
    (out / "results.json").write_text(scrub(json.dumps(redact_obj(probe.results), indent=2, default=str)))
    (out / "report.md").write_text(scrub(render_report(probe)))
    return out


async def main() -> int:
    parser = argparse.ArgumentParser(description="Probe Sky Q boxes and record everything they expose.")
    parser.add_argument("hosts", nargs="*", help="box IP(s); omit to discover via SSDP")
    parser.add_argument("--out", default="probe_results", help="output directory (default: probe_results)")
    parser.add_argument("--ws-seconds", type=float, default=15, help="seconds to listen on each websocket")
    parser.add_argument("--max-ws-messages", type=int, default=25)
    parser.add_argument("--max-descriptions", type=int, default=50, help="UPnP description indexes to try")
    parser.add_argument("--no-cloud", action="store_true", help="skip the Sky cloud EPG comparison")
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--listen", type=int, metavar="SECONDS", help="watch changes while you use the remote")
    parser.add_argument("--key-sweep", action="store_true", help="interactively send undocumented key codes")
    parser.add_argument("--codes", default=DEFAULT_SWEEP, help=f"codes for --key-sweep (default {DEFAULT_SWEEP})")
    parser.add_argument("--include-known", action="store_true", help="also send known codes in the sweep")
    parser.add_argument("--key-batch", metavar="CODES", help="send these codes without prompting (e.g. 3-12)")
    parser.add_argument("--gap", type=float, default=4, help="seconds to watch after each --key-batch code")
    parser.add_argument("--writes", action="store_true", help="run state-changing tests (asks first)")
    parser.add_argument("--pvrid", help="test recording for PVR write tests (e.g. P12345abc)")
    parser.add_argument("--allow-delete", action="store_true", help="include delete+undelete of --pvrid")
    parser.add_argument("--book-eid", help="book this EPG event id as a test recording")
    parser.add_argument("--tune-sid", type=int, help="channel SID for the SOAP tune test (default: retune current)")
    parser.add_argument("--app-launch", metavar="APP_ID", help="try candidate app-launch endpoints with this app")
    parser.add_argument("--yes", action="store_true", help="skip the confirmation prompt for --writes")
    args = parser.parse_args()

    hosts = args.hosts
    if not hosts:
        print("No host given; searching for Sky Q boxes via SSDP…")
        hosts = await asyncio.to_thread(discover)
        if not hosts:
            print("No Sky Q boxes found. Pass the box IP explicitly.")
            return 1
        print(f"Found: {', '.join(hosts)}")

    connector = aiohttp.TCPConnector(limit=8, force_close=True)
    async with aiohttp.ClientSession(connector=connector) as session:
        for host in hosts:
            probe = Probe(host, args, session)
            try:
                await probe.run()
            finally:
                out = write_output(probe, Path(args.out))
            print(f"\n[{host}] report: {out / 'report.md'}")
            for h in probe.results["hypotheses"]:
                print(f"  {mark(h['result']):14} {h['item']}  {h['evidence']}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        sys.exit(130)
