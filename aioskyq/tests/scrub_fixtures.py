"""Remove personal data from fixtures built from a real household's boxes.

Called by build_fixtures.py; safe to run repeatedly. Recording titles, synopses and
programme ids become placeholders, entitlements become a generic set, the regional
sub-bouquet becomes 1, purchase limits are zeroed, favourites become the first ten
channels and the app list keeps only common apps.
"""

import json
import uuid
from pathlib import Path

FIX = Path(__file__).parent / "fixtures"
ENTITLEMENTS = ["HD", "SKY+", "ULTRA+", "GATEWAYENABLER", "PDL", "SKY_DRM_CE", "SKY_DRM_MR"]
KEEP_APPS = {"com.bskyb.epgui", "YouTube", "Netflix", "DisneyPlus", "apple.tv", "com.sky.services"}


def rw_json(path: Path, fn) -> None:
    if not path.exists():
        return
    data = json.loads(path.read_text())
    data = fn(data) or data
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")


def scrub_recording(item: dict) -> None:
    pvrid = item.get("pvrid", "P0")
    if "t" in item:
        item["t"] = f"Recording {pvrid}"
    if "sy" in item:
        item["sy"] = f"Synopsis for {pvrid}."
    for key in ("programmeuuid", "seriesuuid", "seasonuuid"):
        if key in item:
            item[key] = str(uuid.uuid5(uuid.NAMESPACE_URL, f"skyq-fixture:{key}:{pvrid}"))
    for key in ("imageuri_16x9", "imageuri_3x4", "image_16x9"):
        if key in item:
            item[key] = f"https://images.example.invalid/{pvrid}/{key}.jpg"
    if "downloadlink" in item:
        item["downloadlink"] = "oig://00000000"
    if "vodprogrammeid" in item:
        item["vodprogrammeid"] = int(pvrid[1:], 16) % 10_000_000
    if "marketingmessage" in item:
        item["marketingmessage"] = "Marketing message."


def scrub_pvr(data: dict) -> None:
    for item in data.get("pvrItems") or []:
        scrub_recording(item)


def scrub_device(data: dict) -> None:
    if "subbouquet" in data:
        data["subbouquet"] = 1
    for key in ("purchasebalance", "purchaselimit"):
        if key in data:
            data[key] = "0.00"


def scrub_status(data: dict) -> None:
    if "entitlements" in data:
        data["entitlements"] = ENTITLEMENTS
    card = data.get("smartcard")
    if isinstance(card, dict) and "subbouquet" in card:
        card["subbouquet"] = "1"


def scrub_apps(data: dict) -> None:
    data["apps"] = [a for a in data.get("apps", []) if a.get("appId") in KEEP_APPS]


def favourites(_data: dict) -> dict:
    services = json.loads((FIX / "rest/services_bouquet.json").read_text())["services"]
    tv = [s for s in services if s.get("sf") in ("hd", "sd")][:10]
    return {"favourites": [{"lcn": i + 1, "sid": s["sid"]} for i, s in enumerate(tv)]}


def scrub_fixtures() -> None:
    rw_json(FIX / "rest/pvr_list.json", scrub_pvr)
    rw_json(FIX / "rest/apps.json", scrub_apps)
    for kind in ("gateway", "mini"):
        rw_json(FIX / f"{kind}/rest/pvr_changes_current.json", scrub_pvr)
        rw_json(FIX / f"{kind}/rest/system_information.json", scrub_device)
        rw_json(FIX / f"{kind}/rest/system_deviceinformation.json", scrub_device)
        rw_json(FIX / f"{kind}/ws/system_status.json", scrub_status)
        status = FIX / f"{kind}/ws/apps_status.json"
        if status.exists():
            data = json.loads(status.read_text())
            for app in data.get("apps", []):
                if app.get("status") == "VISIBLE" and app.get("appId") not in KEEP_APPS:
                    app["appId"] = "YouTube"
            status.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    fav = FIX / "gateway/rest/services_favourites.json"
    if fav.exists() and json.loads(fav.read_text())["favourites"]:
        rw_json(fav, favourites)


if __name__ == "__main__":
    scrub_fixtures()
