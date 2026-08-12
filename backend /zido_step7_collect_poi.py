# One-line summary: fetches gas station locations and parking lots explicitly tagged "free"
# for all of LA county from OpenStreetMap, and saves them to a CSV in the same format the server already reads.

# =============================================================
#  zido - [Step 8] Gas station + "free" parking location collection (all of LA county)
# -------------------------------------------------------------
#  Location: C:\zido\backend\zido_step8_collect_poi.py
#
#  What this does:
#   Fetches gas station locations, and parking lot locations
#   explicitly tagged "free", for all of LA county from
#   OpenStreetMap and saves them to a file. Uses the exact same
#   approach as zido_step6.
#
#  What "explicitly tagged free" means (important):
#   OSM parking lots may or may not have a fee tag. Out of roughly
#   15,000 parking lots across all of LA county, only about 2,159
#   are tagged "fee=no" (explicitly confirmed free) — most of the
#   rest simply have no fee tag at all (unknown whether free or paid).
#
#   Per A's request to "only show free parking," anything ambiguous
#   is excluded — only entries explicitly tagged fee=no are kept.
#   So this will show far fewer parking spots on the map than
#   actually exist for free — not being on the map doesn't mean
#   "there's no free parking here," it means "OSM doesn't say it's
#   free." Worth reflecting this distinction in the app's wording
#   to avoid confusion.
#
#  Gas stations have no concept of "free" (they're inherently paid),
#  so no fee filter is applied there. Price still isn't in this
#  data — that's instead supplemented by user-submitted reports
#  (cheap_gas reports, sql/zido_fix06).
#
#  The output this file produces can be pulled from the
#  /road-points API with kinds=fuel or kinds=parking, the same way
#  the existing signal/stop-sign data works (no server code changes
#  needed — the server auto-reads any file ending in
#  "..._road_points.csv").
#
#  This can take a while (parking lots especially are numerous, so
#  it may take a few minutes).
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
AREA_NAME = "la_county_poi"

# the exact administrative place name OSM recognizes.
PLACE_NAME = "Los Angeles County, California, USA"

# folder to save output to
SAVE_DIR = Path(r"C:\zido\data")


# =============================================================
#  [1] Decide what to fetch
# -------------------------------------------------------------
#  amenity=fuel → gas station, amenity=parking → parking lot.
#  Both are standard OSM tags.
# =============================================================

WANTED_TAGS = {
    "amenity": ["fuel", "parking"],
}


# =============================================================
#  [2] Fetch the data
# =============================================================
print("=" * 60)
print(f"zido - gas station / parking lot collection ({AREA_NAME})")
print("=" * 60)
print(f"\nArea: {PLACE_NAME}")
print("\n🗺️  Fetching from OpenStreetMap...")
print("   There are a lot of parking lots, so this can take a few minutes. Please wait, don't close this window.")

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
    raise SystemExit()

elapsed = time.time() - start_time
print(f"✅ Got {len(features)} total ({elapsed:.0f}s elapsed)\n")


# =============================================================
#  [3] Extract coordinates
# -------------------------------------------------------------
#  Unlike signals/stop signs, parking lots are much more often
#  drawn as a polygon (the whole lot outlined) rather than a
#  point. For polygons, the centroid is used instead of an exact
#  location — for a large lot the centroid may differ a bit from
#  the actual entrance, but it's good enough for "there's a
#  parking lot around here."
# =============================================================

rows = []   # holds the cleaned-up results
skipped_not_free = 0   # count of parking lots excluded for not being fee=no (for the summary)

for idx, item in features.iterrows():

    geom = item.geometry

    # use the point directly, or the centroid for a polygon/line.
    if geom.geom_type == "Point":
        lat = geom.y
        lon = geom.x
    else:
        center = geom.centroid
        lat = center.y
        lon = center.x

    amenity_value = item.get("amenity") if "amenity" in features.columns else None
    fee_value = item.get("fee") if "fee" in features.columns else None

    kind = None
    if amenity_value == "fuel":
        kind = "fuel"
    elif amenity_value == "parking":
        # per the "only show free parking" request — keep only entries explicitly tagged fee=no
        if fee_value == "no":
            kind = "parking"
        else:
            skipped_not_free += 1

    if kind is None:
        continue

    rows.append({"kind": kind, "lat": lat, "lon": lon})

points = pd.DataFrame(rows)


# =============================================================
#  [4] Show the results
# =============================================================
print("=" * 60)
print("Counts by kind")
print("=" * 60)

counts = points["kind"].value_counts()
for kind_name in counts.index:
    print(f"  {kind_name:<18} {counts[kind_name]:>7}")
print(f"  (for reference) parking lots excluded as not explicitly free: {skipped_not_free}")

print()
print("Sample (first 5)")
print("-" * 60)
print(points.head().to_string(index=False))


# =============================================================
#  [5] Save to a file
# -------------------------------------------------------------
#  The filename must end in "_road_points.csv" for the server to
#  auto-read it (zido_server.py merges every *_road_points.csv it
#  finds in the data folder). The name "points" isn't exclusive to
#  signals/stop signs — it's already used to mean "points on the
#  map" in general, so adding a new kind here is the simplest
#  approach.
# =============================================================

SAVE_DIR.mkdir(parents=True, exist_ok=True)
save_path = SAVE_DIR / f"{AREA_NAME}_road_points.csv"

points.to_csv(save_path, index=False, encoding="utf-8-sig")

print()
print("=" * 60)
print(f"💾 Saved: {save_path}")
print("=" * 60)
print()
print("Next:")
print("  1. restarting or redeploying zido_server.py automatically merges this in")
print("     (verify with /road-points?lat=...&lon=...&kinds=fuel or kinds=parking)")
print("  2. when deploying to Render, this CSV also needs to be committed to git")
print("     (Render's free tier has no disk, so anything not in git disappears on deploy)")
print("  3. price and real-time availability aren't in this data — use it as location markers only")
print(f"  4. parking only includes entries explicitly tagged 'free' (fee=no)"
      f" ({skipped_not_free} were excluded as ambiguous) — not appearing on the map")
print("     doesn't mean there's no free parking there, just that OSM doesn't say so")
