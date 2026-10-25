"""An in-process fake Sky Q box built from real (sanitized) probe fixtures.

It reproduces the behaviour observed on real boxes on 2026-10-25, including the quirks:
SOAP transport control returns 705, the gateway's SkyServe copy of SkyPlay rejects every
action, unknown /status paths accept a websocket but stay silent, deleting a SCHEDULED item
cancels it, and moving the resume position backwards returns 500.
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import json
import re
import time
from pathlib import Path
from typing import Any

from aiohttp import WSMsgType, web

from aioskyq.soap import parse_description

FIXTURES = Path(__file__).parent / "fixtures"
PUSH_PATHS = ("system/status", "apps/status", "pvr/status", "services/status")


def load(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text())


def soap_envelope(action: str, values: dict[str, Any]) -> str:
    body = "".join(f"<{k}>{v}</{k}>" for k, v in values.items())
    return (
        '<?xml version="1.0"?><s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/">'
        f'<s:Body><u:{action}Response xmlns:u="urn:schemas-nds-com:service:SkyPlay:2">{body}'
        f"</u:{action}Response></s:Body></s:Envelope>"
    )


def soap_fault(code: int, description: str) -> str:
    return (
        '<?xml version="1.0"?><s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body>'
        "<s:Fault><faultcode>s:Client</faultcode><faultstring>UPnPError</faultstring><detail>"
        f'<UPnPError xmlns="urn:schemas-upnp-org:control-1-0"><errorCode>{code}</errorCode>'
        f"<errorDescription>{description}</errorDescription></UPnPError></detail></s:Fault>"
        "</s:Body></s:Envelope>"
    )


def error_doc(code: int, message: str, status: int) -> web.Response:
    return web.json_response(
        {"errorCode": code, "userMessage": message, "developerMessage": f"fake box: {message}"}, status=status
    )


class FakeSkyQ:
    def __init__(self, kind: str = "gateway") -> None:
        self.kind = kind
        self.dir = f"{kind}"
        self.standby = False
        self.uri = "xsi://BC0"  # BBC One SE HD, sid 3008
        self.transport = "PLAYING"
        self.visible_app = "com.bskyb.epgui"
        self.pvr = copy.deepcopy(load("rest/pvr_list.json"))
        self.pvr_version = int(self.pvr.get("version") or 1)
        self.keys: list[int] = []
        self.requests: list[tuple[str, str, dict[str, str]]] = []
        self.epg_requests: list[str] = []
        self.page_size_cap: int | None = None  # force paging in tests
        self._sockets: dict[str, set[web.WebSocketResponse]] = {}
        self._next_pvrid = 0x2905000
        self._runners: list[web.AppRunner] = []
        self._remote_server: asyncio.Server | None = None
        self.rest_port = self.upnp_port = self.remote_port = 0
        descriptions = sorted((FIXTURES / kind / "upnp").glob("description*.xml"))
        self.descriptions = {int(re.findall(r"\d+", p.stem)[0]): p.read_text() for p in descriptions}
        # Only the SkyControl device's services work; other copies answer "Invalid action".
        self.working_control_urls = {
            service.control_url
            for index, xml in self.descriptions.items()
            for device in parse_description(xml, index)
            if "SkyControl" in device.device_type
            for service in device.services
        }

    # ------------------------------------------------------------------ lifecycle

    async def start(self) -> FakeSkyQ:
        rest = web.Application()
        rest.router.add_route("*", "/as/{path:.*}", self._rest)
        rest.router.add_get("/hawk/linear/schedule/{day}/{sids}", self._hawk)
        rest.router.add_get("/atlantis/schedule/{day}/{sid}", self._atlantis)
        upnp = web.Application()
        upnp.router.add_get("/{name}", self._upnp_get)
        upnp.router.add_post("/{path:.*}", self._soap)
        self.rest_port = await self._serve(rest)
        self.upnp_port = await self._serve(upnp)
        self._remote_server = await asyncio.start_server(self._remote, "127.0.0.1", 0)
        self.remote_port = self._remote_server.sockets[0].getsockname()[1]
        return self

    async def _serve(self, app: web.Application) -> int:
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        self._runners.append(runner)
        return int(site._server.sockets[0].getsockname()[1])  # type: ignore[union-attr]

    async def stop(self) -> None:
        for sockets in self._sockets.values():
            for ws in list(sockets):
                with contextlib.suppress(Exception):
                    await ws.close()
        if self._remote_server:
            self._remote_server.close()
        for runner in self._runners:
            await runner.cleanup()

    @property
    def hawk_base(self) -> str:
        return f"http://127.0.0.1:{self.rest_port}/hawk/linear"

    @property
    def atlantis_base(self) -> str:
        return f"http://127.0.0.1:{self.rest_port}/atlantis"

    # ------------------------------------------------------------------ push

    async def push(self, path: str, data: dict[str, Any]) -> None:
        for ws in list(self._sockets.get(path, ())):
            await ws.send_str(json.dumps(data))

    async def drop_sockets(self, path: str) -> None:
        for ws in list(self._sockets.get(path, ())):
            await ws.close()

    def snapshot(self, path: str) -> dict[str, Any]:
        data = load(f"{self.kind}/ws/{path.replace('/', '_')}.json")
        if path == "system/status":
            data["systemstate"] = {"state": "standby" if self.standby else "active"}
        if path == "apps/status":
            data = {"apps": [{"appId": self.visible_app, "launchMethod": "LOCAL", "status": "VISIBLE"}]}
        if path == "pvr/status":
            data = {"documentId": self.pvr.get("documentId"), "min": 2, "max": self.pvr_version}
        return data

    # ------------------------------------------------------------------ REST

    def _find(self, pvrid: str | None) -> dict[str, Any] | None:
        return next((i for i in self.pvr["pvrItems"] if i["pvrid"] == pvrid), None)

    async def _rest(self, request: web.Request) -> web.StreamResponse:
        path = request.match_info["path"].rstrip("/")
        query = dict(request.query)
        self.requests.append((request.method, path, query))
        if request.headers.get("Upgrade", "").lower() == "websocket" and path.endswith("/status"):
            return await self._websocket(request, path)
        get = request.method == "GET"
        fixed = {
            "system/deviceinformation": f"{self.kind}/rest/system_deviceinformation.json",
            "system/time": f"{self.kind}/rest/system_time.json",
            "pvr/storage": f"{self.kind}/rest/pvr_storage.json",
            "pin": f"{self.kind}/rest/pin.json",
            "services/favourites": f"{self.kind}/rest/services_favourites.json",
            "services/genres": "rest/services_genres.json",
            "apps": "rest/apps.json",
            "services": "rest/services_bouquet.json",
        }
        if get and path in fixed:
            return web.json_response(load(fixed[path]))
        if get and path == "system/information":
            return web.json_response(
                {**load(f"{self.kind}/rest/system_information.json"), "activeStandby": self.standby}
            )
        if get and re.fullmatch(r"services/\d+/\d+", path):
            return web.json_response(load("rest/services_bouquet.json"))
        if get and (m := re.fullmatch(r"services/details/(\d+)", path)):
            return web.json_response({**load(f"{self.kind}/rest/services_details_0.json"), "sid": m[1]})
        if get and path == "pvr":
            return self._pvr_page(query)
        if get and (m := re.fullmatch(r"pvr/details/(\w+)", path)):
            item = self._find(m[1])
            return web.json_response({"pvrid": m[1], "details": item}) if item else error_doc(105, "Not found", 404)
        if get and re.fullmatch(r"pvr/changes/\w+/\d+", path):
            return web.json_response(load(f"{self.kind}/rest/pvr_changes_current.json"))
        if get and path.startswith("schedule/"):
            return web.json_response(load("rest/error_not_supported.json"), status=500)
        if request.method == "POST" and path.startswith("pvr/action/"):
            return await self._pvr_action(path.rsplit("/", 1)[1], query)
        if request.method == "DELETE" and path == "pvr":
            self.pvr["pvrItems"] = []
            return web.Response(status=200)
        if request.method == "POST" and path == "apps/action/launch":
            self.visible_app = query["appId"]
            await self.push("apps/status", self.snapshot("apps/status"))
            return web.Response(status=200)
        if request.method == "POST" and path == "system/action/ping":
            return web.Response(status=200)
        return web.json_response(load("rest/error_invalid_url.json"), status=404)

    def _pvr_page(self, query: dict[str, str]) -> web.Response:
        items = self.pvr["pvrItems"]
        offset = int(query.get("offset", 0))
        limit = int(query.get("limit", 1000))
        if self.page_size_cap:
            limit = min(limit, self.page_size_cap)
        return web.json_response(
            {
                "documentId": self.pvr.get("documentId"),
                "version": self.pvr_version,
                "offset": offset,
                "totalPvrItems": len(items),
                "pvrItems": items[offset : offset + limit],
            }
        )

    async def _pvr_action(self, action: str, query: dict[str, str]) -> web.Response:
        item = self._find(query.get("pvrid"))
        flags = {
            "keep": ("keep", True),
            "unkeep": ("keep", False),
            "lock": ("lock", True),
            "unlock": ("lock", False),
            "serieslink": ("link", True),
            "seriesunlink": ("link", False),
        }
        if action in ("bookrecording", "bookseriesrecording", "bookppvrecording"):
            self._next_pvrid += 1
            self.pvr["pvrItems"].append(
                {
                    "pvrid": f"P{self._next_pvrid:x}",
                    "status": "SCHEDULED",
                    "oeid": query["eid"],
                    "t": "Booked",
                    "st": int(time.time()) + 3600,
                    "schd": 1800,
                    **({"link": True} if "series" in action else {}),
                }
            )
        elif item is None:
            return error_doc(105, "Not found", 404)
        elif action in flags:
            field, value = flags[action]
            if value:
                item[field] = True
            else:
                item.pop(field, None)  # real boxes omit false flags
        elif action == "delete":
            if item["status"] == "SCHEDULED":
                self.pvr["pvrItems"].remove(item)
            else:
                item["del"] = int(time.time())
        elif action == "undelete":
            item.pop("del", None)
        elif action == "erase":
            self.pvr["pvrItems"].remove(item)
        elif action == "cancelrecording":
            item["status"] = "PART REC"
        elif action == "setlastplayedposition":
            position = int(query["pos"])
            if position < int(item.get("lp") or 0):
                return web.json_response({"errorCode": 500, "userMessage": "Internal error"}, status=500)
            item["lp"] = position
        else:
            return web.json_response(load("rest/error_invalid_url.json"), status=404)
        self.pvr_version += 1
        await self.push("pvr/status", self.snapshot("pvr/status"))
        return web.Response(status=200)

    async def _websocket(self, request: web.Request, path: str) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self._sockets.setdefault(path, set()).add(ws)
        try:
            if path in PUSH_PATHS:  # every other /status path accepts the upgrade and stays silent
                await ws.send_str(json.dumps(self.snapshot(path)))
            async for msg in ws:
                if msg.type == WSMsgType.ERROR:
                    break
        finally:
            self._sockets[path].discard(ws)
        return ws

    # ------------------------------------------------------------------ EPG hosts

    async def _hawk(self, request: web.Request) -> web.Response:
        self.epg_requests.append(f"hawk {request.match_info['sids']}")
        if "X-SkyOTT-Territory" not in request.headers:
            return web.json_response({"errorCode": "400"}, status=400)
        fixture = load("epg/hawk_schedule.json")
        by_sid = {s["sid"]: s["events"] for s in fixture["schedule"]}
        day = request.match_info["day"]
        sids = request.match_info["sids"].split(",")
        events = (lambda sid: by_sid.get(sid, [])) if day == fixture["date"] else (lambda sid: [])
        return web.json_response({"date": day, "schedule": [{"sid": s, "events": events(s)} for s in sids]})

    async def _atlantis(self, request: web.Request) -> web.Response:
        self.epg_requests.append(f"atlantis {request.match_info['sid']}")
        needed = ("X-SkyOTT-Proposition", "X-SkyOTT-Provider", "X-SkyOTT-Territory")
        if any(h not in request.headers for h in needed):
            return web.json_response({"errorCode": "400", "developerMessage": "Missing header(s)"}, status=400)
        fixture = load("epg/atlantis_schedule_uhd.json")
        events = fixture["schedule"][0]["events"] if fixture["schedule"] else []
        return web.json_response(
            {"date": request.match_info["day"], "schedule": [{"sid": request.match_info["sid"], "events": events}]}
        )

    # ------------------------------------------------------------------ UPnP / SOAP

    async def _upnp_get(self, request: web.Request) -> web.Response:
        name = request.match_info["name"]
        if m := re.fullmatch(r"description(\d+)\.xml", name):
            xml = self.descriptions.get(int(m[1]))
            return web.Response(text=xml, content_type="text/xml") if xml else web.Response(status=404)
        return web.Response(status=404)

    async def _soap(self, request: web.Request) -> web.Response:
        action = request.headers.get("SOAPACTION", "").strip('"').split("#")[-1]
        if "/" + request.match_info["path"] not in self.working_control_urls:
            return web.Response(status=500, text=soap_fault(401, "Invalid action"), content_type="text/xml")
        if action == "GetTransportInfo":
            values = {"CurrentTransportState": self.transport, "CurrentTransportStatus": "OK", "CurrentSpeed": 1}
        elif action == "GetMediaInfo":
            values = {**load(f"{self.kind}/soap/SkyPlay_GetMediaInfo.json"), "CurrentURI": self.uri or ""}
        elif action == "GetPositionInfo":
            values = {**load(f"{self.kind}/soap/SkyPlay_GetPositionInfo.json"), "TrackURI": self.uri or ""}
        elif action == "GetCurrentTransportActions":
            values = load(f"{self.kind}/soap/SkyPlay_GetCurrentTransportActions.json")
        elif action in ("Pause", "SetAVTransportURI", "Seek", "Stop"):
            return web.Response(status=500, text=soap_fault(705, "TRANSPORT IS LOCKED"), content_type="text/xml")
        elif action == "Play":
            return web.Response(
                text='<?xml version="1.0"?><s:Envelope xmlns:s="http://schemas.xmlsoap.org/'
                'soap/envelope/"><s:Body/></s:Envelope>',
                content_type="text/xml",
            )
        else:
            return web.Response(status=500, text=soap_fault(401, "Invalid action"), content_type="text/xml")
        return web.Response(
            text=soap_envelope(action, {k: "" if v is None else v for k, v in values.items()}), content_type="text/xml"
        )

    # ------------------------------------------------------------------ remote keys

    async def _remote(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            writer.write(b"SKY 000.001\n")
            await writer.drain()
            await reader.readexactly(12)
            writer.write(b"\x01\x01")
            await writer.drain()
            await reader.readexactly(1)
            writer.write(b"\x00" * 4)
            await writer.drain()
            await reader.readexactly(1)
            writer.write(b"\x00" * 24)
            await writer.drain()
            down = await reader.readexactly(8)
            await reader.readexactly(8)  # key-up
            code = (down[6] - 224) * 16 + down[7]
            self.keys.append(code)
            if code == 0:
                self.standby = not self.standby
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()
