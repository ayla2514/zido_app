# One-line summary: pre-fetches OSM maxspeed tags for every road across all of LA county from
# Overpass and saves them to a file, so the server can look up speed limits instantly instead of calling Valhalla live.

# =============================================================
#  zido - [Step 10] Collect speed limit (maxspeed) data for all of LA county
# -------------------------------------------------------------
#  Location: C:\zido\backend\zido_step10_collect_speed_limits.py
#
#  Why this exists:
#   Until now, /chill-route queried Valhalla (an external paid API)
#   live for every route candidate whenever speed limits were needed
#   (include_speed_limits=true). After seeing how much pre-fetching
#   lane guidance (zido_step9) sped things up, speed limits get the
#   same treatment: fetch OSM's maxspeed tags for all of LA county
#   once ahead of time, save to a file, and have the server look them
#   up instantly from that file.
#
#  maxspeed tags also barely change unless a road is rebuilt, so
#  "occasionally refetch" is enough — no need for "real-time."
#
#  Coverage isn't 100% (a limitation of the underlying data itself —
#  smaller side streets are less likely to be tagged). This limitation
#  already existed when fetching via Valhalla before too — it's not
#  something introduced by switching to this approach.
#
#  One-time setup:
#     pip install requests (probably already installed)
# =============================================================

import requests   # for calling Overpass
import json       # for saving the result
import re         # for extracting the number out of strings like "35 mph"
import time       # for measuring elapsed time
from pathlib import Path


# =============================================================
#  [Config] ← only change values here
# =============================================================

LAT_MIN, LAT_MAX = 33.69, 34.83
LON_MIN, LON_MAX = -118.96, -117.64

OVERPASS_URLS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]

SAVE_PATH = Path(r"C:\zido\data\la_county_speed_limits.json")

OVERPASS_QUERY_TIMEOUT_S = 180


# =============================================================
#  [1] Convert maxspeed values to a mph number
# -------------------------------------------------------------
#  OSM's maxspeed tag comes in inconsistent formats: "35", "35 mph",
#  "60 km/h", "signals" (varies by signal), "none" (no limit, e.g.
#  highways), etc. Since this is the US (LA), a bare number with no
#  unit is treated as mph.
# =============================================================

def parse_maxspeed_to_mph(value):
    """Converts a maxspeed string into an mph number. Returns None if it can't be parsed."""
    if not value:
        return None

    value = value.strip().lower()

    if "mph" in value:
        m = re.search(r"(\d+)", value)
        return int(m.group(1)) if m else None

    if "km/h" in value or "kmh" in value:
        m = re.search(r"(\d+)", value)
        return round(int(m.group(1)) * 0.621371) if m else None

    # a bare number with no unit (US roads) is treated as mph
    if value.isdigit():
        return int(value)

    return None   # values like "signals", "none" aren't numeric and can't be used


# =============================================================
#  [2] Fetch maxspeed-tagged roads for all of LA county
# =============================================================
print("=" * 60)
print("zido - speed limit (maxspeed) data collection (all of LA county)")
print("=" * 60)
print(f"\nBounds: lat {LAT_MIN}~{LAT_MAX} / lon {LON_MIN}~{LON_MAX}")
print("🗺️  Fetching from Overpass... (65,000+ roads, can take a few minutes)")

bbox = f"{LAT_MIN},{LON_MIN},{LAT_MAX},{LON_MAX}"
query = f"""
[out:json][timeout:{OVERPASS_QUERY_TIMEOUT_S}];
way["maxspeed"]({bbox});
out tags geom;
"""

start_time = time.time()
elements = None

for url in OVERPASS_URLS:
    try:
        print(f"   Trying: {url}")
        resp = requests.post(
            url, data={"data": query},
            headers={"User-Agent": "zido-la-driving-app/1.0", "Accept": "application/json"},
            timeout=OVERPASS_QUERY_TIMEOUT_S + 30,
        )
        if resp.status_code == 200:
            elements = resp.json().get("elements", [])
            print(f"   ✅ Success ({url})")
            break
        else:
            print(f"   ⚠️ Failed (status {resp.status_code}), trying the next server")
    except Exception as e:
        print(f"   ⚠️ Failed ({e}), trying the next server")

elapsed = time.time() - start_time

if elements is None:
    print(f"\n❌ All servers failed (gave up after {elapsed:.0f}s)")
    print("   Try again in a bit")
    raise SystemExit()

print(f"\n✅ Got {len(elements)} roads total ({elapsed:.0f}s elapsed)\n")


# =============================================================
#  [3] Reformat into something the server can use directly
# =============================================================

ways = []
skipped_no_geometry = 0
skipped_unparseable = 0

for e in elements:
    geom = e.get("geometry", [])
    if not geom:
        skipped_no_geometry += 1
        continue

    mph = parse_maxspeed_to_mph(e.get("tags", {}).get("maxspeed"))
    if mph is None:
        skipped_unparseable += 1
        continue

    ways.append({
        "points": [[pt["lat"], pt["lon"]] for pt in geom],
        "mph": mph,
    })

print(f"Usable roads: {len(ways)}")
print(f"  ({skipped_no_geometry} excluded for missing geometry, "
      f"{skipped_unparseable} excluded for an unparseable value)")


# =============================================================
#  [4] Save to a file
# =============================================================

SAVE_PATH.parent.mkdir(parents=True, exist_ok=True)
with open(SAVE_PATH, "w", encoding="utf-8") as f:
    json.dump(ways, f, ensure_ascii=False)

file_size_mb = SAVE_PATH.stat().st_size / 1024 / 1024

print()
print("=" * 60)
print(f"💾 Saved: {SAVE_PATH}")
print(f"   File size: {file_size_mb:.1f}MB")
print("=" * 60)
print()
print("Next:")
print("  1. zido_server.py needs to read this file at server startup, and")
print("     look up speed limits directly from it instead of calling Valhalla")
print("  2. when deploying to Render, this file also needs to be committed to git")
