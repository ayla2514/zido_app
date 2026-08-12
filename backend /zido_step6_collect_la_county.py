# One-line summary: fetches traffic signal / stop sign / speed bump locations for all of
# LA county from OpenStreetMap in one shot (queried by place name, "method A") and saves them to a CSV.

# =============================================================
#  zido - [Step 6] Road data collection (all of LA county — method A)
# -------------------------------------------------------------
#  Location: C:\zido\backend\zido_step6_collect_la_county.py
#
#  What this does:
#   Fetches traffic signal / stop sign / speed bump locations for
#   all of LA county from OpenStreetMap in one request and saves
#   them to a file.
#
#  Why this is "method A":
#   zido_step5 fetched a circular radius, which works for a small
#   area. For an entire county, instead of a radius, the whole
#   thing can be requested by "administrative place name"
#   (ox.features_from_place). Since this only fetches point data
#   (not the entire road network of lines), it could be lighter
#   than expected — so this is tried first, before falling back
#   to "method B" (splitting into a grid).
#
#  This can take a while (a few minutes up to ~10 minutes).
#     If it fails partway or takes too long, leave this file as-is
#     and move on to the grid-based "method B" script instead.
#
#  One-time setup:
#     pip install osmnx
# =============================================================

import osmnx as ox          # fetches OpenStreetMap data as tables
import pandas as pd         # the pandas you already know
import time                 # to measure how long this takes
from pathlib import Path


# =============================================================
#  [Config] ← only change values here
# =============================================================

# name of the area being fetched (used in the output filename)
AREA_NAME = "la_county"

# the exact administrative place name OSM recognizes.
# a typo or different spelling causes a "place not found" error.
PLACE_NAME = "Los Angeles County, California, USA"

# folder to save output to
SAVE_DIR = Path(r"C:\zido\data")


# =============================================================
#  [1] Decide what to fetch
# -------------------------------------------------------------
#  Uses the exact same tags as zido_step5, so the kinds line up
#  correctly when the server (zido_server.py) later merges both files.
# =============================================================

WANTED_TAGS = {
    "highway": ["traffic_signals", "stop", "give_way"],
    "traffic_calming": True,
}


# =============================================================
#  [2] Fetch the data
# =============================================================
print("=" * 60)
print(f"zido - road data collection ({AREA_NAME})")
print("=" * 60)
print(f"\nArea: {PLACE_NAME}")
print("\n🗺️  Fetching from OpenStreetMap...")
print("   This covers the whole county, so it can take a few minutes. Please wait, don't close this window.")

start_time = time.time()

try:
    features = ox.features_from_place(
        PLACE_NAME,
        tags=WANTED_TAGS,
    )
except Exception as e:
    elapsed = time.time() - start_time
    print(f"\n❌ Failed (errored after {elapsed:.0f}s)")
    print(f"   Error: {e}")
    print()
    print("   Things to check:")
    print("   1. that the internet connection didn't drop")
    print("   2. that PLACE_NAME is an exact name OSM recognizes")
    print("      (search it directly at nominatim.openstreetmap.org to confirm)")
    print("   3. if it still fails → switch to 'method B' (fetching by grid)")
    print("      the area may just be too large to fetch in one request.")
    raise SystemExit()

elapsed = time.time() - start_time
print(f"✅ Got {len(features)} total ({elapsed:.0f}s elapsed)\n")


# =============================================================
#  [3] Keep only Points
# -------------------------------------------------------------
#  The result also includes lines (roads) and polygons (plazas).
#  Traffic signals and stop signs are single points, so only
#  points are needed.
# =============================================================

features = features[features.geometry.geom_type == "Point"]
print(f"After keeping only points: {len(features)}\n")


# =============================================================
#  [4] Split by kind and extract just the coordinates
# -------------------------------------------------------------
#  Exactly the same approach as zido_step5.
# =============================================================

rows = []   # holds the cleaned-up results

for idx, item in features.iterrows():

    lat = item.geometry.y
    lon = item.geometry.x

    kind = None

    if "highway" in features.columns and pd.notna(item.get("highway")):
        highway_value = item.get("highway")
        if highway_value == "traffic_signals":
            kind = "traffic_signal"
        elif highway_value == "stop":
            kind = "stop_sign"
        elif highway_value == "give_way":
            kind = "give_way"

    if "traffic_calming" in features.columns and pd.notna(item.get("traffic_calming")):
        kind = "speed_bump"

    if kind is None:
        continue

    rows.append({"kind": kind, "lat": lat, "lon": lon})

points = pd.DataFrame(rows)


# =============================================================
#  [5] Show the results
# =============================================================
print("=" * 60)
print("Counts by kind")
print("=" * 60)

counts = points["kind"].value_counts()
for kind_name in counts.index:
    print(f"  {kind_name:<18} {counts[kind_name]:>7}")

print()
print("Sample (first 5)")
print("-" * 60)
print(points.head().to_string(index=False))


# =============================================================
#  [6] Save to a file
# =============================================================

SAVE_DIR.mkdir(parents=True, exist_ok=True)
save_path = SAVE_DIR / f"{AREA_NAME}_road_points.csv"

points.to_csv(save_path, index=False, encoding="utf-8-sig")

print()
print("=" * 60)
print(f"💾 Saved: {save_path}")
print("=" * 60)


# =============================================================
#  [7] Honest check on data quality
# =============================================================

signal_count = int(counts.get("traffic_signal", 0))
stop_count = int(counts.get("stop_sign", 0))

print()
print("📊 Data quality check")
print("-" * 60)
print(f"  traffic signals: {signal_count} / stop signs: {stop_count}")

if stop_count < signal_count:
    print()
    print("  ⚠️ Fewer stop signs than traffic signals came back.")
    print("     Only use relative comparisons like 'route A has more than B'.")
    print("     This is a limitation of the data itself, not something code can fix.")
else:
    print("  Stop sign coverage looks reasonable.")

print()
print("Next:")
print("  1. data/weho_road_points.csv is now covered by this file, so it can be deleted")
print("     (the server merges every *_road_points.csv it finds and auto-dedupes")
print("      overlapping points, so leaving it there causes no immediate problem)")
print("  2. restarting or redeploying zido_server.py will switch it to county-wide data")
print("  3. when deploying to Render, this CSV also needs to be committed to git")
print("     (Render's free tier has no disk, so anything not in git disappears on deploy)")
