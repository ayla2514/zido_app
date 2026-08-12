# One-line summary: pulls traffic signal / stop sign / speed bump locations from OpenStreetMap
# for a small pilot area (West Hollywood) and saves them to a CSV, as a test run before scaling to all of LA county.

# =============================================================
#  zido - [Step 5] Road data collection trial run (West Hollywood)
# -------------------------------------------------------------
#  Location: C:\zido\backend\zido_step5_collect_weho.py
#
#  What this does:
#   Fetches traffic signal / stop sign / speed bump locations for
#   West Hollywood from OpenStreetMap and saves them to a file.
#
#  Why start with a small area:
#   Requesting all of LA county from the start takes 10+ minutes,
#   and if it fails there's no way to tell what went wrong. Getting
#   it working on a small area first, then scaling up, is much faster.
#
#  One-time setup:
#     pip install osmnx
#     mkdir C:\zido\data
# =============================================================

import osmnx as ox          # fetches OpenStreetMap data as tables
import pandas as pd         # the pandas you already know
from pathlib import Path


# =============================================================
#  [Config] ← change values here to cover a different area
# =============================================================

# name of the area being fetched (used in the output filename)
AREA_NAME = "weho"

# area center coordinates (lat, lon) — middle of West Hollywood
CENTER_LAT = 34.0900
CENTER_LON = -118.3617

# radius (meters) around the center to fetch
# 2500m comfortably covers all of West Hollywood
RADIUS_M = 2500

# folder to save output to
SAVE_DIR = Path(r"C:\zido\data")


# =============================================================
#  [1] Decide what to fetch
# -------------------------------------------------------------
#  OSM tags everything on the map. Only the tags we need are
#  requested here.
#
#   highway=traffic_signals  → traffic signal
#   highway=stop             → stop sign
#   highway=give_way         → yield sign
#   traffic_calming=*        → any kind of speed bump
#                              (bump, hump, table, cushion, etc.)
# =============================================================

WANTED_TAGS = {
    "highway": ["traffic_signals", "stop", "give_way"],
    "traffic_calming": True,   # True = "match this tag regardless of its value"
}


# =============================================================
#  [2] Fetch the data
# =============================================================
print("=" * 60)
print(f"zido - road data collection ({AREA_NAME})")
print("=" * 60)
print(f"\nCenter: {CENTER_LAT}, {CENTER_LON}")
print(f"Radius: {RADIUS_M}m")
print("\n🗺️  Fetching from OpenStreetMap... (30s-2min)")

# fetches everything within the radius of the center point.
# the result is a GeoDataFrame — think of it as a pandas DataFrame
# with an extra "geometry" column.
features = ox.features_from_point(
    center_point=(CENTER_LAT, CENTER_LON),
    tags=WANTED_TAGS,
    dist=RADIUS_M,
)

print(f"✅ Got {len(features)} total\n")


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
#  Later, counting "how many stop signs are on this route" only
#  needs kind + coordinates — everything else is dropped.
# =============================================================

rows = []   # holds the cleaned-up results

for idx, item in features.iterrows():

    # extract this point's coordinates.
    # geometry.y is latitude, geometry.x is longitude (easy to mix up).
    lat = item.geometry.y
    lon = item.geometry.x

    # determine the kind
    kind = None

    # use the highway tag if present
    if "highway" in features.columns and pd.notna(item.get("highway")):
        highway_value = item.get("highway")
        if highway_value == "traffic_signals":
            kind = "traffic_signal"
        elif highway_value == "stop":
            kind = "stop_sign"
        elif highway_value == "give_way":
            kind = "give_way"

    # a traffic_calming tag means a speed bump
    if "traffic_calming" in features.columns and pd.notna(item.get("traffic_calming")):
        kind = "speed_bump"

    # skip anything that doesn't match a known kind
    if kind is None:
        continue

    rows.append({"kind": kind, "lat": lat, "lon": lon})

# turn the list into a table
points = pd.DataFrame(rows)


# =============================================================
#  [5] Show the results
# =============================================================
print("=" * 60)
print("Counts by kind")
print("=" * 60)

counts = points["kind"].value_counts()
for kind_name in counts.index:
    print(f"  {kind_name:<18} {counts[kind_name]:>5}")

print()
print("Sample (first 5)")
print("-" * 60)
print(points.head().to_string(index=False))


# =============================================================
#  [6] Save to a file
# -------------------------------------------------------------
#  Re-fetching every time would be slow.
#  Fetch once, save it, and reuse it from here on.
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
# -------------------------------------------------------------
#  Stop signs are under-mapped in OSM for the US — many that
#  exist in reality aren't in the data. Not accounting for this
#  leads to confusing results later ("why does this route show
#  up as comfortable?").
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
    print("     In reality, stop signs vastly outnumber signals on real roads.")
    print("     This means stop signs are under-mapped in OSM here.")
    print()
    print("     → Don't trust absolute counts like 'exactly 7 stop signs'.")
    print("       Only use relative comparisons like 'route A has more than B'.")
    print("       This is a limitation of the data itself, not something code can fix.")
else:
    print("  Stop sign coverage looks reasonable.")

print()
print("Next: open this file and confirm the coordinates fall within West Hollywood.")
print("      (should be around lat 34.07-34.10 / lon -118.40--118.34)")
