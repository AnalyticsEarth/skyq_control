"""Rebuild tests/fixtures from tools/skyq_probe.py output.

    uv run python tests/build_fixtures.py ../probe_results/<gateway_dir> ../probe_results/<mini_dir>

Copies the payloads the tests need, swaps LAN addresses for TEST-NET ones, replaces
device identifiers the probe leaves in UPnP XML (UDN, NDS chip ID, friendly name), trims
the PVR list to a few representative items, and captures live hawk/atlantis EPG samples.
"""

from __future__ import annotations

import json
import re
import sys
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

from scrub_fixtures import scrub_fixtures

OUT = Path(__file__).parent / "fixtures"
FAKE_UUID = "00000000-0000-4000-8000-0000000000{n:02d}"


def load_json(path: Path):
    return json.loads(path.read_text())


def sanitize(text: str, ip_map: dict[str, str], uuid_map: dict[str, str]) -> str:
    for real, fake in ip_map.items():
        text = text.replace(real, fake)
    for real, fake in uuid_map.items():
        text = re.sub(re.escape(real), fake, text, flags=re.IGNORECASE)
    text = re.sub(r"(<(?!/)[^>]*X_NDS_ChipID[^>]*>)[^<]*", r"\g<1>0000000000", text)
    return re.sub(r"<friendlyName>\d+</friendlyName>", "<friendlyName>000000000</friendlyName>", text)


def main(gateway_dir: str, mini_dir: str) -> None:
    gateway, mini = Path(gateway_dir) / "fixtures", Path(mini_dir) / "fixtures"
    texts = "".join(p.read_text(errors="replace") for d in (gateway, mini) for p in d.rglob("*") if p.is_file())
    ips = sorted(set(re.findall(r"\b10\.\d+\.\d+\.\d+\b", texts)))
    ip_map = {ip: f"192.0.2.{i + 10}" for i, ip in enumerate(ips)}
    uuids = sorted(
        {
            u.lower()
            for u in re.findall(
                r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-"
                r"[0-9A-Fa-f]{12}",
                " ".join(p.read_text() for d in (gateway, mini) for p in (d / "upnp").glob("*.xml")),
            )
        }
    )
    uuid_map = {u: FAKE_UUID.format(n=i) for i, u in enumerate(uuids)}

    def put(name: str, data, *, text: bool = False) -> None:
        path = OUT / name
        path.parent.mkdir(parents=True, exist_ok=True)
        raw = data if text else json.dumps(data, indent=2, ensure_ascii=False)
        path.write_text(sanitize(raw, ip_map, uuid_map) + ("" if raw.endswith("\n") else "\n"))

    for label, src in (("gateway", gateway), ("mini", mini)):
        for name in (
            "system_deviceinformation",
            "system_information",
            "system_time",
            "pvr_storage",
            "pin",
            "services_favourites",
            "services_details_0",
            "pvr_changes_current",
        ):
            if (src / "rest" / f"{name}.json").exists():
                put(f"{label}/rest/{name}.json", load_json(src / "rest" / f"{name}.json"))
        for ws in ("system_status", "apps_status", "pvr_status", "services_status"):
            if (src / "ws" / f"{ws}.json").exists():
                put(f"{label}/ws/{ws}.json", load_json(src / "ws" / f"{ws}.json")[0]["data"])
        for soap in src.glob("soap/SkyPlay_*.json"):
            put(f"{label}/soap/{soap.name}", load_json(soap))
        for xml in src.glob("upnp/*.xml"):
            put(f"{label}/upnp/{xml.name}", xml.read_text(), text=True)

    put("rest/apps.json", load_json(gateway / "rest/apps.json"))
    put("rest/services_bouquet.json", load_json(gateway / "rest/services_bouquet.json"))
    put("rest/services_genres.json", load_json(gateway / "rest/explore_services_genres.json"))
    put("rest/error_not_supported.json", load_json(gateway / "rest/schedule_box.json"))
    put("rest/error_invalid_url.json", load_json(gateway / "rest/explore_root.json"))

    pvr = load_json(gateway / "rest/pvr_list.json")
    picked, seen = [], set()
    for item in pvr["pvrItems"]:
        key = (item.get("status"), item.get("src"))
        if key not in seen:
            seen.add(key)
            picked.append(item)
    put("rest/pvr_list.json", {**pvr, "pvrItems": picked, "totalPvrItems": len(picked)})

    services = load_json(gateway / "rest/services_bouquet.json")["services"]
    uhd = next(s["sid"] for s in services if s.get("sf") == "uhd")
    day = datetime.now(UTC).strftime("%Y%m%d")
    for name, url, headers in (
        (
            "epg/hawk_schedule.json",
            f"https://awk.epgsky.com/hawk/linear/schedule/{day}/3008,2075",
            {"X-SkyOTT-Territory": "GB"},
        ),
        (
            "epg/atlantis_schedule_uhd.json",
            f"https://atlantis.epgsky.com/as/schedule/{day}/{uhd}",
            {"X-SkyOTT-Proposition": "SKYQ", "X-SkyOTT-Provider": "SKY", "X-SkyOTT-Territory": "GB"},
        ),
    ):
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=15) as resp:
            put(name, json.load(resp))
    scrub_fixtures()
    print(f"wrote fixtures to {OUT} (ips: {len(ip_map)}, uuids: {len(uuid_map)}, pvr items: {len(picked)})")


if __name__ == "__main__":
    main(*sys.argv[1:3])
