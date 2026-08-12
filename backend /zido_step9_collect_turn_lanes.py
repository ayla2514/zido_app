# One-line summary: pre-fetches every turn:lanes-tagged road across all of LA county from
# Overpass and saves it to a file, so the server can look up lane guidance instantly instead of calling Overpass live.

# =============================================================
#  zido - [Step 9] Collect turn:lanes data for all of LA county
# -------------------------------------------------------------
#  Location: C:\zido\backend\zido_step9_collect_turn_lanes.py
#
#  Why this exists:
#   Until now, zido_server.py called Overpass (free map API) live on
#   every /chill-route request, asking "are there any turn:lanes-tagged
#   roads near here?" Since Overpass is sometimes slow or fails
#   (B reported this as slowness), repeating this up to 3 times per
#   route candidate meant a single request could take over 30s in the
#   worst case.
#
#   Same fix as already applied to signals/stop signs/gas stations/
#   street sweeping data (fetch it all once ahead of time and save to
#   a file → the server looks it up instantly from that file). This
#   applies the same approach to turn:lanes data, so the server never
#   needs to call Overpass at request time at all.
#
#  turn:lanes tags are static data that rarely changes unless a road
#  gets rebuilt, so "occasionally refetch" is plenty — no need for
#  "real-time."
#
#  This can take a while (all of LA county, 37,000+ roads, including
#  each road's full geometry — heavier than the signal/stop-sign collection).
#
#  One-time setup:
#     pip install requests (probably already installed)
# =============================================================

import requests   # for calling Overpass
import json       # for saving the result
import time       # for measuring elapsed time
from pathlib import Path


# =============================================================
#  [Config] ← only change values here
# =============================================================

# LA county bounds (excluding islands) — same as zido_server.py's LA_LAT_MIN etc.
LAT_MIN, LAT_MAX = 33.69, 34.83
LON_MIN, LON_MAX = -118.96, -117.64

OVERPASS_URLS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]

SAVE_PATH = Path(r"C:\zido\data\la_county_turn_lanes.json")

# Overpass's own timeout (seconds). Set generously since this fetches
# the whole county plus road geometry — this is a one-time script, so
# taking a while is fine.
OVERPASS_QUERY_TIMEOUT_S = 180


# =============================================================
#  [1] Fetch turn:lanes-tagged roads for all of LA county
# =============================================================
print("=" * 60)
print("zido - turn:lanes data collection (all of LA county)")
print("=" * 60)
print(f"\nBounds: lat {LAT_MIN}~{LAT_MAX} / lon {LON_MIN}~{LON_MAX}")
print("🗺️  Fetching from Overpass... (37,000+ roads, can take a few minutes)")

bbox = f"{LAT_MIN},{LON_MIN},{LAT_MAX},{LON_MAX}"
query = f"""
[out:json][timeout:{OVERPASS_QUERY_TIMEOUT_S}];
(
  way["turn:lanes"]({bbox});
  way["turn:lanes:forward"]({bbox});
  way["turn:lanes:backward"]({bbox});
);
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
    print("   Try again in a bit (Overpass may be temporarily busy)")
    raise SystemExit()

print(f"\n✅ Got {len(elements)} roads total ({elapsed:.0f}s elapsed)\n")


# =============================================================
#  [2] Reformat into something the server can use directly
# -------------------------------------------------------------
#  Saves data in the exact same shape (points, tags) that
#  zido_server.py's fetch_turn_lanes() used to get directly from
#  Overpass — that way the server code only needs to change "read
#  from this file instead of Overpass," and none of the lane
#  interpretation logic needs to change at all.
# =============================================================

ways = []
skipped_no_geometry = 0

for e in elements:
    geom = e.get("geometry", [])
    if not geom:
        skipped_no_geometry += 1
        continue
    ways.append({
        "points": [[pt["lat"], pt["lon"]] for pt in geom],
        "tags": e.get("tags", {}),
    })

print(f"After keeping only roads with geometry: {len(ways)} "
      f"({skipped_no_geometry} excluded for missing geometry)")


# =============================================================
#  [3] Save to a file
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
print("  1. update zido_server.py so that instead of calling Overpass on")
print("     every request, it loads this file into memory once at server")
print("     startup and looks things up directly from there (next step)")
print("  2. when deploying to Render, this file also needs to be committed to git")
print("  3. turn:lanes rarely changes, so rerunning this script every few")
print("     months to refresh it is enough")
