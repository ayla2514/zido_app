# One-line summary: turns LA city's official street sweeping schedule (which only has
# text street-boundary descriptions, no coordinates) into rough rectangular map zones by geocoding each boundary street.

# =============================================================
#  zido - [Step 9] Street sweeping zone collection
# -------------------------------------------------------------
#  Location: C:\zido\backend\zido_step9_collect_street_sweeping.py
#
#  What this does:
#   Fetches "this neighborhood has street sweeping on this day
#   between these hours, no parking allowed then" info from LA
#   city's official public dataset ("Posted Street Sweeping
#   Routes"), and converts it into map-usable coordinates (zones)
#   for saving.
#
#  Why this is so involved (please read):
#   This official dataset has no coordinates at all. Instead, a
#   "boundaries" field just has a human-readable sentence like
#   "Chandler Bl. to Ventura Fwy / Colfax Av. to Laurel Cyn. Bl."
#   meaning "the area bounded by these 4 streets."
#
#   So this script:
#   1) splits that sentence into 4 street names (formatting is
#      inconsistent, so some rows fail to parse — failed rows are
#      just dropped, never guessed at)
#   2) geocodes each street name's coordinates using a free
#      geocoder (Nominatim, OSM)
#   3) only saves a zone as a "rectangle" if all 4 street
#      coordinates were found — if even one is missing, that row
#      is dropped
#   4) also drops a zone if its rectangle is too large (likely
#      means a street name was mistakenly matched to a different area)
#
#   Even after all that, this is still "neighborhood (rectangle)"
#   accuracy, not "specific street segment" accuracy. It can't say
#   "don't park on this exact block" — it should only be used as a
#   rough warning like "parking enforcement happens somewhere in
#   this area at this time." The app must never confidently show
#   "parking guaranteed here" — being wrong could mean an actual
#   parking ticket for the user.
#
#  This takes a long time (~700 street names × 1s each = 10+
#     minutes). Nominatim (the free geocoder) has a rule against
#     more than 1 request per second, so this deliberately runs
#     slowly. Please wait and don't close this window.
#
#  One-time setup:
#     pip install requests (probably already installed)
# =============================================================

import requests   # for calling LA city's open data + the Nominatim geocoder
import re         # for splitting the boundaries sentence into street names
import time       # for waiting 1s between Nominatim requests
import json       # for saving the result
import math       # for computing zone rectangle size (diagonal distance)
from pathlib import Path


# =============================================================
#  [Config] ← only change values here
# =============================================================

# LA city's official street sweeping open dataset (Socrata API, free, no key needed)
SOCRATA_URL = "https://data.lacity.org/resource/krk7-ayq2.json"

# Nominatim (OpenStreetMap's free geocoder). Must respect the 1-request-per-second rule
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
NOMINATIM_DELAY_SEC = 1.1   # slightly more than the 1/sec rule, for safety

# bounding box to bias geocoding results toward (all of LA county)
# matches the service area in CLAUDE.md section 8
LA_LAT_MIN, LA_LAT_MAX = 33.69, 34.83
LA_LON_MIN, LA_LON_MAX = -118.96, -117.64

# a zone rectangle larger than this gets dropped (likely means a
# street name was matched to the wrong place — "only keep what's certain")
MAX_ZONE_DIAGONAL_M = 15000

# where to save the output
SAVE_PATH = Path(r"C:\zido\data\street_sweeping_zones.json")

# weekday abbreviation -> full name
DAY_MAP = {"M": "Mon", "Tu": "Tue", "W": "Wed", "Th": "Thu", "F": "Fri", "Sa": "Sat", "Su": "Sun"}


# =============================================================
#  [1] Fetch the raw data from LA city
# =============================================================
print("=" * 60)
print("zido - street sweeping zone collection")
print("=" * 60)
print("\n[1/4] Fetching LA city's open data...")

resp = requests.get(SOCRATA_URL, params={"$limit": 2000}, timeout=30,
                     headers={"User-Agent": "zido-la-driving-app-research/1.0"})
resp.raise_for_status()
raw_rows = resp.json()
print(f"  Got {len(raw_rows)} rows")


# =============================================================
#  [2] Split each row into "day + time + 4 street names"
# -------------------------------------------------------------
#  Rows with a format mismatch (ambiguous) are dropped. Never
#  forcing an uncertain row through is the core principle here.
# =============================================================
print("\n[2/4] Parsing each row (extracting day / street names)...")


def parse_day(route_no):
    """Finds the weekday abbreviation at the end of route_no. Returns None if absent."""
    m = re.search(r"(Su|Sa|Th|Tu|M|W|F)\.?\s*$", route_no.strip())
    return DAY_MAP.get(m.group(1)) if m else None


def split_boundary_pairs(boundaries):
    """Splits a sentence like 'X to Y / A to B' into [(X,Y), (A,B)].
    Returns an empty list if the format doesn't match (that row gets
    dropped later). This data was hand-entered by people over many
    years, so at least two formats are mixed in (e.g. separated by
    "to" / separated only by "-"), handled as two cases here."""
    if re.search(r"\bto\b", boundaries, flags=re.I):
        # format with "to": segments separated by / , or - (before a letter/digit)
        segments = re.split(r"[/,]|-\s*(?=[A-Za-z0-9])", boundaries)
        pairs = []
        for seg in segments:
            parts = re.split(r"\bto\b", seg, flags=re.I)
            if len(parts) == 2 and parts[0].strip(" .") and parts[1].strip(" ."):
                pairs.append((parts[0].strip(" ."), parts[1].strip(" .")))
        return pairs
    else:
        # format without "to": segments separated by ',', '-' acts as "to"
        pairs = []
        for seg in boundaries.split(","):
            parts = seg.split("-")
            if len(parts) == 2 and parts[0].strip(" .") and parts[1].strip(" ."):
                pairs.append((parts[0].strip(" ."), parts[1].strip(" .")))
        return pairs


parsed_rows = []          # rows that parsed successfully
street_names_needed = set()  # every unique street name that needs geocoding
skip_no_day = 0
skip_no_pairs = 0

for row in raw_rows:
    day = parse_day(row.get("route_no", ""))
    pairs = split_boundary_pairs(row.get("boundaries", "") or "")

    if day is None:
        skip_no_day += 1
        continue
    if len(pairs) != 2:
        skip_no_pairs += 1
        continue

    street_names = [pairs[0][0], pairs[0][1], pairs[1][0], pairs[1][1]]
    for name in street_names:
        street_names_needed.add(name)

    parsed_rows.append({
        "route_no": row.get("route_no"),
        "cd": row.get("cd"),
        "day": day,
        "time_start": row.get("time_start"),
        "time_end": row.get("time_end"),
        "street_names": street_names,
        "boundaries_raw": row.get("boundaries"),
    })

print(f"  Parsed successfully: {len(parsed_rows)} rows")
print(f"  Dropped — no weekday found: {skip_no_day} rows, "
      f"4-street format mismatch: {skip_no_pairs} rows")
print(f"  Unique street names to geocode: {len(street_names_needed)}")


# =============================================================
#  [3] Geocode coordinates for each street name (Nominatim, free)
# -------------------------------------------------------------
#  This is the slowest part, due to the 1-request-per-second rule.
# =============================================================
print(f"\n[3/4] Geocoding {len(street_names_needed)} street names "
      f"(1 per second, so about {len(street_names_needed) // 60} min)...")

street_coords = {}   # street name -> (lat, lon), or absent entirely if not found

for i, name in enumerate(sorted(street_names_needed)):
    try:
        r = requests.get(NOMINATIM_URL, params={
            "q": f"{name}, Los Angeles County, CA",
            "format": "json",
            "limit": 1,
            "viewbox": f"{LA_LON_MIN},{LA_LAT_MAX},{LA_LON_MAX},{LA_LAT_MIN}",
            "bounded": 1,
        }, headers={"User-Agent": "zido-la-driving-app-research/1.0"}, timeout=15)
        results = r.json()
        if results:
            street_coords[name] = (float(results[0]["lat"]), float(results[0]["lon"]))
    except Exception:
        pass   # treat a failure as "not found" (automatically dropped below)

    time.sleep(NOMINATIM_DELAY_SEC)

    if (i + 1) % 50 == 0:
        print(f"  processed {i + 1}/{len(street_names_needed)} "
              f"(found {len(street_coords)} so far)")

print(f"  Streets geocoded: {len(street_coords)} / {len(street_names_needed)}")


# =============================================================
#  [4] Only save rows where all 4 street coordinates were found
# =============================================================
print("\n[4/4] Computing zone rectangles...")


def haversine_m(lat1, lon1, lat2, lon2):
    """Distance (meters) between two coordinates. Only used to check whether a rectangle is too big."""
    R = 6371000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


zones = []
skip_missing_coord = 0
skip_too_big = 0

for row in parsed_rows:
    coords = [street_coords.get(name) for name in row["street_names"]]
    if any(c is None for c in coords):
        skip_missing_coord += 1
        continue

    lats = [c[0] for c in coords]
    lons = [c[1] for c in coords]
    lat_min, lat_max = min(lats), max(lats)
    lon_min, lon_max = min(lons), max(lons)

    diagonal_m = haversine_m(lat_min, lon_min, lat_max, lon_max)
    if diagonal_m > MAX_ZONE_DIAGONAL_M:
        skip_too_big += 1
        continue

    zones.append({
        "route_no": row["route_no"],
        "cd": row["cd"],
        "day": row["day"],
        "time_start": row["time_start"],
        "time_end": row["time_end"],
        "lat_min": lat_min, "lat_max": lat_max,
        "lon_min": lon_min, "lon_max": lon_max,
        "boundaries_raw": row["boundaries_raw"],
    })

print(f"  Final zones: {len(zones)}")
print(f"  Dropped — street coordinates not found: {skip_missing_coord} rows, "
      f"rectangle larger than {MAX_ZONE_DIAGONAL_M}m (likely mismatched): {skip_too_big} rows")


# =============================================================
#  [5] Save + honest summary of results
# =============================================================
SAVE_PATH.parent.mkdir(parents=True, exist_ok=True)
with open(SAVE_PATH, "w", encoding="utf-8") as f:
    json.dump(zones, f, ensure_ascii=False, indent=2)

print()
print("=" * 60)
print(f"Saved: {SAVE_PATH}")
print("=" * 60)
print(f"Out of {len(raw_rows)} original rows, only {len(zones)} zones could be confidently kept"
      f" ({len(zones) * 100 // max(len(raw_rows), 1)}%).")
print()
print("⚠️ Reminder: this is 'neighborhood (rectangle)' granularity, not 'street segment'.")
print("   The app should only use this as a warning, like 'this area may have")
print("   parking enforcement Tuesday 9-11 AM' — never state 'parking is fine here'.")
print()
print("Next:")
print("  1. zido_server.py needs an endpoint added that reads this file and")
print("     finds nearby zones (not built yet)")
print("  2. when deploying to Render, this JSON file also needs to be committed to git")
