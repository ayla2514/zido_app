# One-line summary: FastAPI backend for zido — serves road-point data (signals/stop signs/bumps),
# live road closures/alerts/air quality, nearby free parking, and computes "chill" (low-fatigue) driving routes.

# =============================================================
#  zido - API server (v0.5.1)
# -------------------------------------------------------------
#  Location: C:\zido\backend\zido_server.py
#
#  Endpoints:
#   GET  /               server info
#   GET  /health         health check
#   GET  /road-points    traffic signal / stop sign / speed bump locations
#   GET  /road-closures  NEW — live road closures (Caltrans, state highways only)
#   GET  /road-alerts    NEW (experimental) — CHP live incidents + chain control (opt-in, not in main flow)
#   GET  /air-quality    NEW (experimental) — AQI, needs AirNow key (free signup, not yet configured)
#   GET  /parking-near   NEW — free parking + street parking zones near a destination (approximate)
#   POST /chill-route    3 chill routes (ranked by score) — scored with real data
#   POST /reroute        quick single-route recompute when the driver goes off-route
#
#  Version history:
#   v0.2   — /road-points (for drawing map icons)
#   v0.3   — wired real signal/stop-sign/bump data into chill-route scoring
#   v0.4   — added /reroute (handles going off-route mid-drive)
#   v0.4.1 — fixed error responses being double-wrapped as { "detail": {...} }
#            (now sends { "error", "message" } directly, as documented).
#            /reroute timeout also reduced 30s -> 12s (can't wait long while driving)
#   v0.4.2 — perf: pre-split road points by kind into POINTS_BY_KIND so
#            find_nearby/count_near_route no longer filter kind out of
#            80k+ rows every call — split once at startup and reuse
#   v0.5.0 — investigated/implemented 3 requests from B:
#            1) added /road-closures — free Caltrans feed (state highways only,
#               doesn't include LA city's own streets). Data mixes construction
#               with maintenance/incident cleanup, so honestly labeled "closure"
#               rather than "construction"
#            2) lane guidance — not available from Valhalla, so added by
#               fetching and parsing OSM turn:lanes tags directly
#               (steps[].lane_guidance, optional)
#            3) speed limits — added via Valhalla trace_attributes
#               (steps[].speed_limit_mph, optional). Gaps in the data are
#               filled in from neighboring values, and speed_limit_source
#               marks whether a value is inferred or actual
#            2) and 3) require an extra request so they slow things down —
#            only enabled when include_speed_limits / include_lane_guidance
#            is sent as true (off by default)
#   v0.5.1 — toll/highway avoidance (picked only the Waze-like features that
#            don't conflict with zido's philosophy and are free). Sending
#            avoid_tolls / avoid_highways as true feeds directly into the
#            Valhalla route calculation (no extra API call, no speed impact).
#            has_toll/has_highway are now always included on every route.
#            Gas stations / parking locations added via /road-points
#            kinds=fuel / kinds=parking. Parking only includes spots
#            explicitly tagged "free" (fee=no) — price and real-time
#            availability aren't freely available, so only location is
#            given (see data/la_county_poi_road_points.csv)
#            Gas prices supplemented by user-submitted reports
#            (cheap_gas reports, sql/zido_fix06)
#            New /parking-near — free parking + street sweeping zones near
#            a destination. Street sweeping zones have no coordinates in
#            LA city's official data, so they're approximated as rectangles
#            (zido_step9) — turned out much bigger than a "neighborhood"
#            (median diagonal ~10km), nowhere close to street-segment or
#            even neighborhood granularity — response always includes a
#            disclaimer, and uncertain zones are excluded entirely
#
#  Why /reroute is separate:
#   /chill-route compares 3 candidates to recommend the most comfortable
#   one, which takes a bit longer. When the driver goes off-route mid-drive,
#   getting a fast new route matters more than comparing options, so this
#   lighter version fetches just 1 candidate.
#
#  One-time setup:
#     pip install fastapi uvicorn requests python-dotenv polyline pandas
#
#  Run:
#     cd C:\zido\backend
#     python -m uvicorn zido_server:app --reload --port 8000
# =============================================================

import os
import re                                # used to parse Caltrans KML
import math
import time
import json                              # used to read street_sweeping_zones.json
import requests
import polyline
import pandas as pd
import numpy as np                       # comes bundled with pandas already, no
                                          # separate install needed. Used for
                                          # vectorized (matrix) distance calculations.
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo            # gives exact "what day/time is it in LA right now"
                                          # (built into Python, no install needed)
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, HTMLResponse
from pydantic import BaseModel


# =============================================================
#  [Config] ← only change values here
# =============================================================

env_path = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(dotenv_path=env_path)

STADIA_API_KEY = os.getenv("STADIA_API_KEY")

# NEW (experimental) — air quality (AQI). Free but requires a separate signup
# key that A hasn't created yet. If missing from .env, /air-quality just
# honestly reports "not configured" (doesn't crash the server or affect
# other features).
# See docs/zido_B_전달_전체정리.md for how to get the key.
AIRNOW_API_KEY = os.getenv("AIRNOW_API_KEY")

# --- routing ---
NUM_ALTERNATIVES = 3       # number of route candidates
MANEUVER_PENALTY = 30      # how much to dislike turns (higher = prefers simpler routes)

# scoring weights (matches CLAUDE.md section 4 scoring table)
# adjust these numbers if real-world driving feel doesn't match
W_UNPROTECTED_LEFT = 40   # unsignaled left turn — hardest, must check both directions
W_PROTECTED_LEFT   = 3    # signaled left turn — just wait for the arrow, much easier
W_UTURN            = 40
W_LANE             = 25
W_STOP_SIGN        = 8
W_SPEED_BUMP       = 4
W_PER_MIN          = 2

# radius (meters) within which a road point counts as "nearby"
LEFT_TURN_SIGNAL_RADIUS_M = 25   # signal within 25m of a left turn counts as protected
STOP_SIGN_RADIUS_M        = 30   # stop signs within 30m of the route
SPEED_BUMP_RADIUS_M       = 30   # speed bumps within 30m of the route

# --- road point data ---
# reads and merges every "*_road_points.csv" file in the data folder.
# no code changes needed even as more LA county tile files get added later.
DATA_DIR = Path(__file__).resolve().parent.parent / "data"

MAX_RADIUS_M = 3000        # max radius allowed per request
MAX_POINTS   = 500         # max points returned per request
                           # (drawing thousands of pins would lag the app)

# --- street sweeping zones ---
# precomputed output file from zido_step9_collect_street_sweeping.py.
# not real-time data (a weekly recurring schedule), so like road_points,
# it's loaded into memory once at server startup.
STREET_SWEEPING_PATH = DATA_DIR / "street_sweeping_zones.json"
PARKING_NEAR_MAX_RADIUS_M = 1500   # wider than road points since this is destination-based
LA_TIMEZONE = ZoneInfo("America/Los_Angeles")   # server runs in UTC but "current LA time" uses this

# --- road closures (Caltrans) ---
# fetching statewide data on every request would be slow, so it's cached
# in memory and only refetched after this many minutes.
CALTRANS_KML_URL = "https://quickmap.dot.ca.gov/data/lcs2way.kml"
CLOSURE_CACHE_MINUTES = 20
CLOSURE_MAX_RADIUS_M = 10000   # highway closures matter from farther away than stop signs, so wider radius

# --- NEW (experimental) — CHP live incidents + chain control (same Caltrans QuickMap, same format) ---
# not wired into the app's main flow yet. Exposed as a separate endpoint
# (/road-alerts) for B to use optionally (per A's request).
CHP_KML_URL = "https://quickmap.dot.ca.gov/data/chp-only.kml"
CHAIN_CONTROL_KML_URL = "https://quickmap.dot.ca.gov/data/cc.kml"
ROAD_ALERTS_CACHE_MINUTES = 5   # CHP incidents change much faster than closures, so shorter cache
ROAD_ALERTS_MAX_RADIUS_M = 10000

# --- speed limits (Valhalla trace_attributes) ---
# 2026-08-10: B reported "/chill-route is slow." Traced it to
# include_speed_limits/include_lane_guidance querying new areas
# (this request fires once per route candidate — up to 3 — and on failure
# waits out the full timeout), reproduced cases over 30s in the worst case.
# This is a "nice to have" optional feature, so it's better to fail fast
# and return null than wait a long time — timeout cut significantly
# (15s -> 6s).
SPEED_LIMIT_TIMEOUT_S = 6   # optional request, so give up quickly if it's slow

# --- LA county bounds (excluding islands) ---
LA_LAT_MIN, LA_LAT_MAX = 33.69, 34.83
LA_LON_MIN, LA_LON_MAX = -118.96, -117.64

# --- for distance calculations ---
# 1 degree of latitude is roughly 111320m everywhere on Earth.
# 1 degree of longitude varies with latitude (widest at the equator, 0 at the poles).
# so longitude needs a cos() correction.
METERS_PER_DEG_LAT = 111320


# =============================================================
#  [Reference] Valhalla maneuver type codes
# -------------------------------------------------------------
#   9 slight right  10 right  11 sharp right
#  14 sharp left    15 left   16 slight left      ← left-turn group
#  12 u-turn(right) 13 u-turn(left)                ← most to avoid
#  17 stay on ramp  ← NOT a left turn! easy to confuse
#  18/19 ramp  20/21 exit  23/24 keep lane  25 merge  ← lane-change stress
# =============================================================

LEFT_TURN_TYPES   = [14, 15, 16]
UTURN_TYPES       = [12, 13]
LANE_STRESS_TYPES = [18, 19, 20, 21, 23, 24, 25]

# The app itself is in English, so these values are all in English.
# (Only the # comment on this block is Korean — the values below are
# shown directly in the app, so they're kept in English.)
INSTRUCTIONS = {
    1:  "Starting your trip",
    2:  "Starting your trip, turn right",
    3:  "Starting your trip, turn left",
    4:  "You've arrived 🎉",
    5:  "You've arrived, on the right 🎉",
    6:  "You've arrived, on the left 🎉",
    7:  "Continue straight ahead",
    8:  "Go straight",
    9:  "Bear right",
    10: "Turn right",
    11: "Turn sharp right",
    12: "Make a U-turn",
    13: "Make a U-turn",
    14: "Turn sharp left",
    15: "Turn left 🍃",
    16: "Bear left",
    17: "Continue on the ramp",
    18: "Take the ramp on the right",
    19: "Take the ramp on the left",
    20: "Take the exit on the right",
    21: "Take the exit on the left",
    22: "Go straight",
    23: "Keep right",
    24: "Keep left",
    25: "Merge",
    26: "Enter the roundabout",
    27: "Exit the roundabout",
}


# =============================================================
#  [1] Load road data into memory at server startup
# -------------------------------------------------------------
#  Reading files on every request would be slow.
#  Read once at server startup and keep it in memory.
#
#  Render's free tier has 512MB memory and no disk. So the data
#  files need to live in the git repo, and only the needed
#  columns should be kept to stay lightweight.
# =============================================================

print("📂 Loading road point data...")

loaded_frames = []   # holds all the CSVs we read

if DATA_DIR.exists():
    for csv_path in sorted(DATA_DIR.glob("*_road_points.csv")):
        try:
            one = pd.read_csv(csv_path)
            loaded_frames.append(one)
            print(f"   ✅ {csv_path.name} — {len(one)} rows")
        except Exception as e:
            print(f"   ⚠️ failed to read {csv_path.name}: {e}")
else:
    print(f"   ⚠️ data folder not found: {DATA_DIR}")

if len(loaded_frames) > 0:
    road_points = pd.concat(loaded_frames, ignore_index=True)

    # keep only needed columns to save memory
    road_points = road_points[["kind", "lat", "lon"]]

    # de-dupe in case the same point appears in multiple files
    road_points = road_points.drop_duplicates()

    # downcast coords to float32 — plenty of precision, half the memory
    road_points["lat"] = road_points["lat"].astype("float32")
    road_points["lon"] = road_points["lon"].astype("float32")

    # precompute the actual coverage bounds of the data.
    # used to answer "is there data for this area?" queries from the app.
    DATA_LAT_MIN = float(road_points["lat"].min())
    DATA_LAT_MAX = float(road_points["lat"].max())
    DATA_LON_MIN = float(road_points["lon"].min())
    DATA_LON_MAX = float(road_points["lon"].max())

    print(f"   {len(road_points)} points ready")
    print(f"   coverage: lat {DATA_LAT_MIN:.4f}~{DATA_LAT_MAX:.4f} / "
          f"lon {DATA_LON_MIN:.4f}~{DATA_LON_MAX:.4f}")
else:
    # server must still start even without data.
    # /chill-route still works without it.
    road_points = pd.DataFrame(columns=["kind", "lat", "lon"])
    DATA_LAT_MIN = DATA_LAT_MAX = DATA_LON_MIN = DATA_LON_MAX = 0.0
    print("   ⚠️ no road point data — /road-points will return empty results")

# perf: pre-split by kind.
# find_nearby/count_near_route are called dozens of times per request;
# instead of filtering "kind == traffic_signal" out of 80k+ rows each
# time, split once at startup and reuse the split.
POINTS_BY_KIND = {}
if len(road_points) > 0:
    for kind_name in road_points["kind"].unique():
        POINTS_BY_KIND[kind_name] = road_points[road_points["kind"] == kind_name].reset_index(drop=True)


# =============================================================
#  [1-1] Load street sweeping zones
# -------------------------------------------------------------
#  Loads the JSON produced by zido_step9 into memory as-is. If
#  missing, the server still starts fine (/parking-near just
#  returns an empty street_sweeping_zones list; nothing else
#  is affected).
# =============================================================

STREET_SWEEPING_ZONES = []
if STREET_SWEEPING_PATH.exists():
    try:
        with open(STREET_SWEEPING_PATH, encoding="utf-8") as f:
            STREET_SWEEPING_ZONES = json.load(f)
        print(f"🅿️  loaded {len(STREET_SWEEPING_ZONES)} street sweeping zones")
    except Exception as e:
        print(f"   ⚠️ failed to read street sweeping zones: {e}")
else:
    print("   ⚠️ no street sweeping zone data — /parking-near will return an empty list")


# =============================================================
#  [1-1-1] Helper — pre-bucket ways into a coordinate grid
# -------------------------------------------------------------
#  Both lane guidance and speed limits repeatedly ask "what road
#  is near this coordinate?" With 60k-110k ways, scanning all of
#  them every time is slow (measured up to 32s on a long route).
#  So ways are pre-bucketed into small grid cells ahead of time —
#  then a lookup only needs to check "the ~9 cells around mine"
#  instead of every way in existence.
# =============================================================

WAY_GRID_CELL_SIZE = 0.01   # grid cell size in degrees, ~1km


def build_way_grid_index(ways):
    """Pre-buckets a list of ways into grid cells."""
    grid = {}
    for way in ways:
        cells_seen = set()
        for lat, lon in way["points"]:
            cell = (round(lat / WAY_GRID_CELL_SIZE), round(lon / WAY_GRID_CELL_SIZE))
            if cell not in cells_seen:
                cells_seen.add(cell)
                grid.setdefault(cell, []).append(way)
    return grid


def query_way_grid(grid, lat, lon):
    """Quickly pulls candidate ways from the 3x3 grid cells around this coordinate."""
    base_lat = round(lat / WAY_GRID_CELL_SIZE)
    base_lon = round(lon / WAY_GRID_CELL_SIZE)
    seen_ids = set()
    candidates = []
    for dlat in (-1, 0, 1):
        for dlon in (-1, 0, 1):
            for way in grid.get((base_lat + dlat, base_lon + dlon), []):
                wid = id(way)
                if wid not in seen_ids:
                    seen_ids.add(wid)
                    candidates.append(way)
    return candidates


# =============================================================
#  [1-2] NEW — preload lane guidance (turn:lanes) data
# -------------------------------------------------------------
#  2026-08-10: B reported "/chill-route is slow" — caused by
#  calling Overpass (free map API) live on every request. Same
#  fix as for signals/stop signs/gas stations: zido_step11
#  fetches all of LA county's turn:lanes data once ahead of time
#  and saves it to a file, so the server just reads that file —
#  no Overpass calls at request time at all. If the file is
#  missing (zido_step11 hasn't been run yet), falls back
#  automatically to the old live-fetch method — see
#  fetch_turn_lanes.
# =============================================================

TURN_LANES_PATH = DATA_DIR / "la_county_turn_lanes.json"
TURN_LANES_WAYS = None   # None = "no file -> fall back to live Overpass calls"
TURN_LANES_GRID = None   # fast-lookup grid index (built below)

if TURN_LANES_PATH.exists():
    try:
        with open(TURN_LANES_PATH, encoding="utf-8") as f:
            TURN_LANES_WAYS = json.load(f)
        TURN_LANES_GRID = build_way_grid_index(TURN_LANES_WAYS)
        print(f"🛣️  loaded {len(TURN_LANES_WAYS)} lane-guidance ways (no live Overpass calls needed)")
    except Exception as e:
        print(f"   ⚠️ failed to read lane guidance data: {e}")
else:
    print("   ⚠️ no lane guidance data — will call Overpass live per request "
          "(run zido_step11_collect_turn_lanes.py to speed this up)")


# =============================================================
#  [1-3] NEW — preload speed limit (maxspeed) data
# -------------------------------------------------------------
#  Same reasoning as lane guidance: speed limits are now looked
#  up from a file pre-fetched by zido_step12 instead of calling
#  Valhalla live on every request. Falls back automatically to
#  the old live-fetch method if the file is missing.
# =============================================================

SPEED_LIMITS_PATH = DATA_DIR / "la_county_speed_limits.json"
SPEED_LIMIT_WAYS = None   # None = "no file -> fall back to live Valhalla calls"
SPEED_LIMIT_GRID = None   # fast-lookup grid index (built below)

if SPEED_LIMITS_PATH.exists():
    try:
        with open(SPEED_LIMITS_PATH, encoding="utf-8") as f:
            SPEED_LIMIT_WAYS = json.load(f)
        SPEED_LIMIT_GRID = build_way_grid_index(SPEED_LIMIT_WAYS)
        print(f"🚗 loaded {len(SPEED_LIMIT_WAYS)} speed-limit ways (no live Valhalla calls needed)")
    except Exception as e:
        print(f"   ⚠️ failed to read speed limit data: {e}")
else:
    print("   ⚠️ no speed limit data — will call Valhalla live per request "
          "(run zido_step12_collect_speed_limits.py to speed this up)")


# =============================================================
#  [1-1] Road point proximity helpers
# -------------------------------------------------------------
#  "Is there a signal/stop sign nearby?" is used in several
#  places (/road-points and /chill-route). Pulled out into
#  shared functions to avoid duplicating this logic.
# =============================================================

def _points_for_kinds(kinds):
    """Pulls and merges only the road points matching `kinds` from the
    pre-split lookup. Thanks to POINTS_BY_KIND, this avoids scanning
    all 80k+ rows every time — it grabs only the needed kinds directly."""
    if kinds is None:
        return road_points
    parts = [POINTS_BY_KIND[k] for k in kinds if k in POINTS_BY_KIND]
    if len(parts) == 0:
        return road_points.iloc[0:0]
    if len(parts) == 1:
        return parts[0]
    return pd.concat(parts, ignore_index=True)


def find_nearby(lat, lon, radius_m, kinds=None):
    """Finds road points within radius_m (meters) of the given coordinate.

    The returned table includes a distance_m column.
    If kinds is omitted, all kinds are searched.
    """
    subset = _points_for_kinds(kinds)

    if len(subset) == 0:
        empty = subset.copy()
        empty["distance_m"] = []
        return empty

    # step 1: cheap rectangular pre-filter
    meters_per_deg_lon = METERS_PER_DEG_LAT * math.cos(math.radians(lat))
    lat_margin = radius_m / METERS_PER_DEG_LAT
    lon_margin = radius_m / meters_per_deg_lon

    nearby = subset[
        (subset["lat"] >= lat - lat_margin) & (subset["lat"] <= lat + lat_margin)
        & (subset["lon"] >= lon - lon_margin) & (subset["lon"] <= lon + lon_margin)
    ].copy()

    if len(nearby) == 0:
        nearby["distance_m"] = []
        return nearby

    # step 2: measure actual distance on the remaining rows (Pythagorean)
    delta_lat_m = (nearby["lat"] - lat) * METERS_PER_DEG_LAT
    delta_lon_m = (nearby["lon"] - lon) * meters_per_deg_lon
    nearby["distance_m"] = (delta_lat_m ** 2 + delta_lon_m ** 2) ** 0.5
    return nearby[nearby["distance_m"] <= radius_m]


# 2026-08-10 bug fix: B reported Render (free tier, 512MB memory) crashing
# with 502 on long routes across LA county (~40km). Cause: the matrix
# calculation below loads "candidate count × route point count" into
# memory at once — long routes have a bigger bbox, so candidates can reach
# thousands (8000+ stop signs alone) and route points can also number in
# the hundreds to thousands, making the matrix hundreds of MB (measured
# 226MB directly). chill-route repeats this up to 3 candidates ×
# (stop signs + bumps) = 6 times, making it worse. Didn't crash locally
# (plenty of memory) — only crashed on Render, which delayed discovery.
# Fix: process route points in chunks of ROUTE_CHUNK_SIZE instead of all
# at once. Result is identical (each chunk's "was it within range" is
# OR'd together), but memory drops to "candidate count × chunk size".
ROUTE_CHUNK_SIZE = 200


def count_near_route(coords, radius_m, kinds):
    """Counts road points that are within radius_m of ANY point along
    the whole route (coords).

    Different from find_nearby: find_nearby checks a single point,
    this checks the entire route — needed to catch stop signs on
    plain straight segments that have no turn instruction nearby.
    """
    if len(coords) == 0:
        return 0

    candidates_all = _points_for_kinds(kinds)
    if len(candidates_all) == 0:
        return 0

    route_lat = np.array([c[0] for c in coords])
    route_lon = np.array([c[1] for c in coords])
    avg_lat = float(route_lat.mean())
    meters_per_deg_lon = METERS_PER_DEG_LAT * math.cos(math.radians(avg_lat))
    lat_margin = radius_m / METERS_PER_DEG_LAT
    lon_margin = radius_m / meters_per_deg_lon

    # step 1: cheap rectangular pre-filter around the whole route (+ margin)
    candidates = candidates_all[
        (candidates_all["lat"] >= route_lat.min() - lat_margin)
        & (candidates_all["lat"] <= route_lat.max() + lat_margin)
        & (candidates_all["lon"] >= route_lon.min() - lon_margin)
        & (candidates_all["lon"] <= route_lon.max() + lon_margin)
    ]
    if len(candidates) == 0:
        return 0

    # step 2: for each candidate, find distance to the nearest route point.
    # Route points are processed in chunks of ROUTE_CHUNK_SIZE, so only a
    # small (candidate count × chunk size) matrix is held in memory at
    # once. Each chunk's "within radius_m" result is OR'd together —
    # identical result to computing the whole route at once, but with
    # far less memory.
    cand_lat = candidates["lat"].to_numpy()[:, None]
    cand_lon = candidates["lon"].to_numpy()[:, None]
    found = np.zeros(len(candidates), dtype=bool)

    for start in range(0, len(route_lat), ROUTE_CHUNK_SIZE):
        chunk_lat = route_lat[start:start + ROUTE_CHUNK_SIZE]
        chunk_lon = route_lon[start:start + ROUTE_CHUNK_SIZE]

        dlat_m = (cand_lat - chunk_lat[None, :]) * METERS_PER_DEG_LAT
        dlon_m = (cand_lon - chunk_lon[None, :]) * meters_per_deg_lon
        chunk_min_dist = ((dlat_m ** 2 + dlon_m ** 2) ** 0.5).min(axis=1)

        found |= (chunk_min_dist <= radius_m)

    return int(found.sum())


# =============================================================
#  [2] Create the server
# =============================================================

app = FastAPI(
    title="zido API",
    description="Finds the most stress-free driving route in LA",
    version="0.5.0",
)


# FastAPI by default wraps raise HTTPException(detail={...}) in an extra
# layer as { "detail": {...} }. All the docs (CLAUDE.md, docs/) were
# written assuming the unwrapped { "error": ..., "message": ... } shape,
# but that wasn't actually happening — this strips the extra wrapper so
# actual behavior matches the docs.
@app.exception_handler(HTTPException)
async def strip_detail_wrapper(request: Request, exc: HTTPException):
    return JSONResponse(status_code=exc.status_code, content=exc.detail)


class RouteRequest(BaseModel):
    start: list[float]   # [lat, lon]
    end: list[float]     # [lat, lon]
    # both optional (default false). Turning them on adds one (or more)
    # extra request, which slows things down — only enable when needed.
    include_speed_limits: bool = False
    include_lane_guidance: bool = False
    # v0.5.1 — toll/highway avoidance (default false = same as before).
    # No extra request; folded into the Valhalla route calculation itself,
    # so no speed impact.
    avoid_tolls: bool = False
    avoid_highways: bool = False


# =============================================================
#  [3] Info / health check
# -------------------------------------------------------------
#  Hitting the bare root URL and seeing "Not Found" could make
#  someone think the server is down, so this gives basic info instead.
# =============================================================

@app.get("/")
def root():
    return {
        "service": "zido API",
        "version": "0.5.1",
        "endpoints": ["/health", "/road-points", "/road-closures", "/road-alerts", "/air-quality", "/parking-near", "/chill-route", "/reroute", "/docs"],
        "road_points_loaded": len(road_points),
    }


@app.get("/health")
def health():
    return {"status": "ok", "road_points_loaded": len(road_points)}


# =============================================================
#  A's personal use — LATER guide (public page, no login needed)
# -------------------------------------------------------------
#  Completely unrelated to B's app API (deliberately left out of
#  the endpoint list). Originally a button page linking to a Claude
#  artifact, but artifacts are private and require login — so this
#  was changed to have the server read the JSON in data/LATER/
#  directly and render the page itself. No login required at all.
#  Updating the JSON in data/LATER/ (e.g. via an "event research"
#  trigger) and deploying automatically reflects on this page —
#  no code changes needed.
# =============================================================

LATER_TEMPLATE_PATH = Path(__file__).resolve().parent / "later_site_template.html"
LATER_DATA_DIR = DATA_DIR / "LATER"

@app.get("/later", response_class=HTMLResponse)
def later_guide():
    try:
        template = LATER_TEMPLATE_PATH.read_text(encoding="utf-8")
        cafes = (LATER_DATA_DIR / "cafes_hotspots" / "la_new_cafes.json").read_text(encoding="utf-8")
        events = (LATER_DATA_DIR / "free_events" / "la_free_events.json").read_text(encoding="utf-8")
        dine = (LATER_DATA_DIR / "dine_la" / "dine_la_2026_summer.json").read_text(encoding="utf-8")
    except FileNotFoundError as e:
        return HTMLResponse(f"<p>Couldn't find LATER data file: {e}</p>", status_code=500)

    html = (template
            .replace("__CAFES_JSON__", cafes)
            .replace("__EVENTS_JSON__", events)
            .replace("__DINE_JSON__", dine))
    return HTMLResponse(html)


# =============================================================
#  [4] NEW — serve road points
# -------------------------------------------------------------
#  Lets the app draw traffic signals/stop signs/speed bumps
#  directly on the map.
#
#  Example usage:
#   /road-points?lat=34.09&lon=-118.3617&radius_m=800
#   /road-points?lat=34.09&lon=-118.3617&radius_m=800&kinds=stop_sign
#
#  The app should call this with the map's current center
#  coordinates whenever the map moves.
# =============================================================

@app.get("/road-points")
def get_road_points(
    lat: float = Query(..., description="Center latitude"),
    lon: float = Query(..., description="Center longitude"),
    radius_m: int = Query(800, description=f"Radius in meters, max {MAX_RADIUS_M}"),
    kinds: str = Query(None, description="Comma-separated. Omit for all kinds"),
):

    # ---------------------------------------------------------
    #  4-1. validate input
    # ---------------------------------------------------------
    check_in_la_or_raise(lat, lon)

    # cap the radius — a large radius is slow for the server and
    # unusable for the app (can't draw thousands of pins anyway)
    if radius_m > MAX_RADIUS_M:
        radius_m = MAX_RADIUS_M
    if radius_m < 50:
        radius_m = 50

    # ---------------------------------------------------------
    #  4-2. check whether this area has data
    # ---------------------------------------------------------
    #  All of LA county is currently collected so this rarely
    #  triggers, but kept as a safeguard in case of gaps in the
    #  data or a different collection area later.
    # ---------------------------------------------------------
    has_data = len(road_points) > 0
    covered = (
        has_data
        and DATA_LAT_MIN <= lat <= DATA_LAT_MAX
        and DATA_LON_MIN <= lon <= DATA_LON_MAX
    )

    if not covered:
        if has_data:
            note = (f"This area isn't collected yet. Current coverage: "
                    f"lat {DATA_LAT_MIN:.4f}~{DATA_LAT_MAX:.4f} / "
                    f"lon {DATA_LON_MIN:.4f}~{DATA_LON_MAX:.4f}")
        else:
            note = "No road point data yet"

        return {
            "center": [lat, lon],
            "radius_m": radius_m,
            "covered": False,
            "coverage_note": note,
            "count": 0,
            "total_found": 0,
            "truncated": False,
            "counts_by_kind": {},
            "points": [],
        }

    # ---------------------------------------------------------
    #  4-3 & 4-4. find points within radius + filter by kind
    # -------------------------------------------------------------
    #  Uses the find_nearby() helper (rectangular pre-filter, then
    #  actual distance — a two-step approach fast enough for the
    #  hundreds of thousands of points across all of LA county).
    #  /chill-route uses the same function.
    # ---------------------------------------------------------
    wanted_kinds = None
    if kinds is not None:
        wanted_kinds = [k.strip() for k in kinds.split(",") if k.strip() != ""]

    nearby = find_nearby(lat, lon, radius_m, kinds=wanted_kinds)

    # ---------------------------------------------------------
    #  4-5. cap the count (closest first)
    # ---------------------------------------------------------
    total_found = len(nearby)
    truncated = False

    if total_found > MAX_POINTS:
        nearby = nearby.nsmallest(MAX_POINTS, "distance_m")
        truncated = True

    # ---------------------------------------------------------
    #  4-6. build the response
    # ---------------------------------------------------------
    points = []
    for _, row in nearby.iterrows():
        points.append({
            "kind": row["kind"],
            "lat": round(float(row["lat"]), 6),
            "lon": round(float(row["lon"]), 6),
            "distance_m": int(row["distance_m"]),
        })

    # also send counts per kind.
    # used by the app for things like "12 stop signs nearby"
    kind_counts = {}
    for p in points:
        kind_counts[p["kind"]] = kind_counts.get(p["kind"], 0) + 1

    return {
        "center": [lat, lon],
        "radius_m": radius_m,
        "covered": True,
        "coverage_note": None,
        "count": len(points),
        "total_found": total_found,
        "truncated": truncated,
        "counts_by_kind": kind_counts,
        "points": points,
    }


# =============================================================
#  [4-1] NEW — live road closures (Caltrans)
# -------------------------------------------------------------
#  Uses Caltrans' (California DOT) free public lane/road closure
#  feed.
#  This data is NOT labeled "construction only" — it also includes
#  maintenance, incident cleanup, and emergency work, so `kind`
#  only distinguishes closure type (full closure / partial lane
#  closure), and the exact reason is given as free text in `reason`.
#  This data covers California "state" highways/arterials (like
#  405, 10, 101) — not LA city's own streets (side streets etc).
#  LA city's own construction work isn't captured here.
#  Statewide data is slow to refetch every request, so it's cached
#  in memory and only refreshed after CLOSURE_CACHE_MINUTES.
# =============================================================

_closure_cache = {"fetched_at": None, "closures": []}


def parse_caltrans_kml(xml_text, kind_from_style=None):
    """Converts Caltrans KML into a list we can work with easily.
    Only entries with Point info are used (route-line-only entries are skipped).

    kind_from_style — a function that maps styleUrl (e.g. "full-closure")
    to our own kind name. Defaults to the road-closure rule
    (full_closure/lane_closure) if omitted. Pulled out so it can be
    reused for other feeds like CHP incidents/chain control — the
    format (iw-title/iw-text/Point etc.) is identical, so the parsing
    logic itself doesn't need to change."""
    if kind_from_style is None:
        kind_from_style = lambda style: "full_closure" if "full-closure" in style else "lane_closure"

    closures = []
    for p in re.findall(r"<Placemark>(.*?)</Placemark>", xml_text, re.S):
        point_m = re.search(r"<Point>\s*<coordinates>([^<]+)</coordinates>", p)
        if not point_m:
            continue

        parts = point_m.group(1).strip().split(",")
        try:
            lon, lat = float(parts[0]), float(parts[1])
        except (ValueError, IndexError):
            continue

        style_m = re.search(r"styleUrl>#([^<]+)<", p)
        style = style_m.group(1) if style_m else "lcs"
        kind = kind_from_style(style)

        title_m = re.search(r'iw-title">([^<]+)</h2>', p)
        reason_parts = [r.strip() for r in re.findall(r'iw-text">([^<]*)</p>', p) if r.strip()]
        updated_m = re.search(r"Last updated: <strong>([^<]+)</strong>", p)

        closures.append({
            "kind": kind,
            "lat": lat,
            "lon": lon,
            "title": title_m.group(1).strip() if title_m else None,
            "reason": " / ".join(reason_parts) if reason_parts else None,
            "updated": updated_m.group(1) if updated_m else None,
        })
    return closures


def get_cached_closures():
    """Refetches from Caltrans if the cache is stale, otherwise uses the
    cache as-is. Falls back to the old cache on fetch failure so the
    server never crashes over this."""
    now = time.time()
    is_stale = (
        _closure_cache["fetched_at"] is None
        or (now - _closure_cache["fetched_at"]) > CLOSURE_CACHE_MINUTES * 60
    )
    if is_stale:
        try:
            resp = requests.get(
                CALTRANS_KML_URL, timeout=15, headers={"User-Agent": "zido/1.0"}
            )
            resp.raise_for_status()
            _closure_cache["closures"] = parse_caltrans_kml(resp.text)
            _closure_cache["fetched_at"] = now
        except Exception as e:
            print(f"   ⚠️ failed to refresh Caltrans closures: {e}")
            # fetched_at is left alone so the next request retries.
    return _closure_cache["closures"]


@app.get("/road-closures")
def get_road_closures(
    lat: float = Query(..., description="Center latitude"),
    lon: float = Query(..., description="Center longitude"),
    radius_m: int = Query(3000, description=f"Radius in meters, max {CLOSURE_MAX_RADIUS_M}"),
):
    check_in_la_or_raise(lat, lon)

    if radius_m > CLOSURE_MAX_RADIUS_M:
        radius_m = CLOSURE_MAX_RADIUS_M
    if radius_m < 200:
        radius_m = 200

    all_closures = get_cached_closures()

    meters_per_deg_lon = METERS_PER_DEG_LAT * math.cos(math.radians(lat))
    lat_margin = radius_m / METERS_PER_DEG_LAT
    lon_margin = radius_m / meters_per_deg_lon

    nearby = []
    for c in all_closures:
        if not (lat - lat_margin <= c["lat"] <= lat + lat_margin
                and lon - lon_margin <= c["lon"] <= lon + lon_margin):
            continue
        dlat_m = (c["lat"] - lat) * METERS_PER_DEG_LAT
        dlon_m = (c["lon"] - lon) * meters_per_deg_lon
        dist = (dlat_m ** 2 + dlon_m ** 2) ** 0.5
        if dist <= radius_m:
            nearby.append({**c, "distance_m": int(dist)})

    nearby.sort(key=lambda x: x["distance_m"])

    cache_age_s = int(time.time() - _closure_cache["fetched_at"]) if _closure_cache["fetched_at"] else None

    return {
        "center": [lat, lon],
        "radius_m": radius_m,
        "count": len(nearby),
        "closures": nearby,
        "source": "Caltrans QuickMap (California state highways only)",
        "source_cache_age_s": cache_age_s,
    }


# =============================================================
#  [4-0] NEW (experimental) — CHP live incidents + chain control
# -------------------------------------------------------------
#  A asked to actively investigate air quality/CHP incidents/chain
#  control, but not wire them into the main app flow yet — instead
#  expose them for B to opt into. So this is completely absent from
#  /chill-route and /reroute; whether B calls this endpoint at all
#  is entirely up to B.
#
#  Uses the exact same Caltrans QuickMap service and KML format as
#  /road-closures, so it reuses parse_caltrans_kml above (just
#  extracts `kind` differently).
#
#  `kind` values are passed through exactly as Caltrans' own style
#  names in the raw KML ("chp"/"emergency"/"notclosed"/"full-closure"
#  etc.) — unlike road closures, we did NOT invent new names based
#  on a guess of what they mean. Since the exact meaning couldn't be
#  confirmed, passing the original value through as-is felt more
#  honest than guessing a new label.
# =============================================================

_chp_cache = {"fetched_at": None, "items": []}
_chain_control_cache = {"fetched_at": None, "items": []}


def get_cached_kml_feed(cache, url, cache_minutes, label):
    """Shared caching logic for CHP incidents / chain control — same
    pattern as /road-closures' get_cached_closures, just parameterized
    by url/cache dict/label so both feeds can reuse it."""
    now = time.time()
    is_stale = (
        cache["fetched_at"] is None
        or (now - cache["fetched_at"]) > cache_minutes * 60
    )
    if is_stale:
        try:
            resp = requests.get(url, timeout=15, headers={"User-Agent": "zido/1.0"})
            resp.raise_for_status()
            cache["items"] = parse_caltrans_kml(resp.text, kind_from_style=lambda style: style)
            cache["fetched_at"] = now
        except Exception as e:
            print(f"   ⚠️ failed to refresh {label}: {e}")
    return cache["items"]


@app.get("/road-alerts")
def get_road_alerts(
    lat: float = Query(..., description="Center latitude"),
    lon: float = Query(..., description="Center longitude"),
    radius_m: int = Query(5000, description=f"Radius in meters, max {ROAD_ALERTS_MAX_RADIUS_M}"),
    kinds: str = Query("chp_incident,chain_control",
                        description="Comma-separated: chp_incident, chain_control"),
):
    check_in_la_or_raise(lat, lon)

    if radius_m > ROAD_ALERTS_MAX_RADIUS_M:
        radius_m = ROAD_ALERTS_MAX_RADIUS_M
    if radius_m < 200:
        radius_m = 200

    wanted = [k.strip() for k in kinds.split(",") if k.strip()]

    all_items = []
    if "chp_incident" in wanted:
        for item in get_cached_kml_feed(_chp_cache, CHP_KML_URL, ROAD_ALERTS_CACHE_MINUTES, "CHP incidents"):
            all_items.append({**item, "category": "chp_incident"})
    if "chain_control" in wanted:
        for item in get_cached_kml_feed(_chain_control_cache, CHAIN_CONTROL_KML_URL,
                                         ROAD_ALERTS_CACHE_MINUTES, "chain control"):
            all_items.append({**item, "category": "chain_control"})

    meters_per_deg_lon = METERS_PER_DEG_LAT * math.cos(math.radians(lat))
    lat_margin = radius_m / METERS_PER_DEG_LAT
    lon_margin = radius_m / meters_per_deg_lon

    nearby = []
    for a in all_items:
        if not (lat - lat_margin <= a["lat"] <= lat + lat_margin
                and lon - lon_margin <= a["lon"] <= lon + lon_margin):
            continue
        dlat_m = (a["lat"] - lat) * METERS_PER_DEG_LAT
        dlon_m = (a["lon"] - lon) * meters_per_deg_lon
        dist = (dlat_m ** 2 + dlon_m ** 2) ** 0.5
        if dist <= radius_m:
            nearby.append({**a, "distance_m": int(dist)})

    nearby.sort(key=lambda x: x["distance_m"])

    return {
        "center": [lat, lon],
        "radius_m": radius_m,
        "count": len(nearby),
        "alerts": nearby,
        "source": "Caltrans QuickMap — CHP incidents statewide, chain control mostly mountain routes",
        "note": ("Experimental / opt-in — not used by /chill-route or /reroute. "
                 "kind values are Caltrans' own style names, passed through as-is."),
    }


# =============================================================
#  [4-0-1] NEW (experimental) — air quality (AQI)
# -------------------------------------------------------------
#  Uses the AirNow (US EPA) API. Completely free but requires
#  signing up at airnowapi.org to get a key — if A hasn't created
#  one yet, this endpoint honestly reports "not configured" instead
#  of erroring or crashing the server.
#  Once a key exists, just add AIRNOW_API_KEY to .env to enable it.
#  Also not wired into /chill-route or /reroute — a separate opt-in
#  call for B to use if wanted.
# =============================================================

@app.get("/air-quality")
def get_air_quality(
    lat: float = Query(..., description="Latitude"),
    lon: float = Query(..., description="Longitude"),
):
    check_in_la_or_raise(lat, lon)

    if not AIRNOW_API_KEY:
        return {
            "available": False,
            "message": "AIRNOW_API_KEY not set on the server yet — "
                       "sign up free at airnowapi.org and add it to .env",
        }

    try:
        resp = requests.get(
            "https://www.airnowapi.org/aq/observation/latLong/current/",
            params={
                "format": "application/json",
                "latitude": lat,
                "longitude": lon,
                "distance": 25,
                "API_KEY": AIRNOW_API_KEY,
            },
            timeout=10,
        )
        resp.raise_for_status()
        readings = resp.json()
    except Exception as e:
        print(f"   ⚠️ AirNow request failed: {e}")
        return {"available": False, "message": "AirNow request failed"}

    return {
        "available": True,
        "center": [lat, lon],
        "readings": [
            {
                "parameter": r.get("ParameterName"),   # "PM2.5" / "OZONE" etc.
                "aqi": r.get("AQI"),
                "category": r.get("Category", {}).get("Name"),   # "Good"/"Moderate"/...
                "observed_at": f'{r.get("DateObserved")} {r.get("HourObserved")}:00',
            }
            for r in readings
        ],
        "source": "AirNow (US EPA)",
    }


# =============================================================
#  [4-1] NEW — free parking + street sweeping near a destination (v0.5.1)
# -------------------------------------------------------------
#  Built at A's request: "show free parking or same-day street
#  parking availability within 500m of the destination."
#
#  Important limitation for street parking specifically:
#  the zones built by zido_step9 aren't per-street-segment — they're
#  much wider "rectangles" (median diagonal ~10km — bigger than a
#  "neighborhood"). So "restricted" here means "somewhere in this
#  rectangle may be a sweeping time" — NOT "this block is 100%
#  off-limits." Likewise, not being flagged "restricted" is no
#  guarantee parking is fully allowed (could be a different zone
#  nearby, or a rule this data doesn't cover at all, like meters or
#  permit zones). A disclaimer is always included in the response —
#  the app must never present this as a confident "you can park
#  here" without that caveat.
# =============================================================

def parse_clock_time(text):
    """Converts a string like '10:00 AM' into an (hour, minute) tuple. Returns None on failure."""
    try:
        t = datetime.strptime(text.strip(), "%I:%M %p")
        return (t.hour, t.minute)
    except Exception:
        return None


def zone_overlaps_circle(zone, lat, lon, radius_m):
    """Checks whether a zone (rectangle) overlaps the radius around the
    center point, using the distance from the center to the nearest
    point of the rectangle."""
    nearest_lat = min(max(lat, zone["lat_min"]), zone["lat_max"])
    nearest_lon = min(max(lon, zone["lon_min"]), zone["lon_max"])

    meters_per_deg_lon = METERS_PER_DEG_LAT * math.cos(math.radians(lat))
    dlat_m = (nearest_lat - lat) * METERS_PER_DEG_LAT
    dlon_m = (nearest_lon - lon) * meters_per_deg_lon
    return (dlat_m ** 2 + dlon_m ** 2) ** 0.5 <= radius_m


def zone_currently_restricted(zone, now_la):
    """Checks whether right now (LA time) falls within this zone's
    sweeping window."""
    weekday_names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    if weekday_names[now_la.weekday()] != zone["day"]:
        return False

    start = parse_clock_time(zone["time_start"])
    end = parse_clock_time(zone["time_end"])
    if start is None or end is None:
        return False

    now_minutes = now_la.hour * 60 + now_la.minute
    start_minutes = start[0] * 60 + start[1]
    end_minutes = end[0] * 60 + end[1]
    return start_minutes <= now_minutes <= end_minutes


@app.get("/parking-near")
def parking_near(
    lat: float = Query(..., description="Destination latitude"),
    lon: float = Query(..., description="Destination longitude"),
    radius_m: int = Query(500, description=f"Radius in meters, max {PARKING_NEAR_MAX_RADIUS_M}"),
):
    check_in_la_or_raise(lat, lon)

    if radius_m > PARKING_NEAR_MAX_RADIUS_M:
        radius_m = PARKING_NEAR_MAX_RADIUS_M
    if radius_m < 50:
        radius_m = 50

    # --- free parking lots (only ones explicitly tagged fee=no, see zido_step8) ---
    free_parking = find_nearby(lat, lon, radius_m, kinds=["parking"])
    free_parking_list = [
        {"lat": float(r["lat"]), "lon": float(r["lon"]), "distance_m": int(r["distance_m"])}
        for _, r in free_parking.iterrows()
    ]

    # --- street sweeping zones (wide rectangular approximation, see above) ---
    # Zones turned out much bigger than expected (median diagonal ~10km —
    # more like "several neighborhoods combined" than a single
    # neighborhood). So even a small radius can overlap multiple zones.
    # To make this at least somewhat useful, smaller (more specific) zones
    # are sorted first.
    now_la = datetime.now(LA_TIMEZONE)
    zones_nearby = []
    for zone in STREET_SWEEPING_ZONES:
        if not zone_overlaps_circle(zone, lat, lon, radius_m):
            continue
        area_span = (zone["lat_max"] - zone["lat_min"]) * (zone["lon_max"] - zone["lon_min"])
        zones_nearby.append({
            "day": zone["day"],
            "time_start": zone["time_start"],
            "time_end": zone["time_end"],
            "currently_restricted": zone_currently_restricted(zone, now_la),
            "area": [[zone["lat_min"], zone["lon_min"]], [zone["lat_max"], zone["lon_max"]]],
            "_area_span": area_span,   # for sorting, stripped before returning
        })

    zones_nearby.sort(key=lambda z: z["_area_span"])
    for z in zones_nearby:
        del z["_area_span"]

    return {
        "center": [lat, lon],
        "radius_m": radius_m,
        "checked_at_la_time": now_la.strftime("%a %I:%M %p"),
        "free_parking": free_parking_list,
        "street_sweeping_zones": zones_nearby,
        "disclaimer": (
            "street_sweeping_zones covers a wide area (median ~10km across), "
            "not a specific block or street — treat it as a general regional "
            "warning only, ordered smallest/most-specific area first. Never "
            "claim parking is guaranteed safe based on this data alone."
        ),
    }


# =============================================================
#  [5] Find the chill route
# =============================================================

def check_in_la_or_raise(lat, lon):
    """Raises an error if the coordinate is outside LA county. Used by both /chill-route and /reroute."""
    in_la = (LA_LAT_MIN <= lat <= LA_LAT_MAX) and (LA_LON_MIN <= lon <= LA_LON_MAX)
    if not in_la:
        raise HTTPException(
            status_code=400,
            detail={"error": "OUT_OF_LA", "message": "zido is only available in LA 🌴"}
        )


def request_trips(start_lat, start_lon, end_lat, end_lon, num_alternates, timeout_s=30,
                   avoid_tolls=False, avoid_highways=False):
    """Asks Valhalla for route candidates from A to B, and returns the list of trips.

    Lowering num_alternates (0 = single candidate) makes the response
    faster. /chill-route needs multiple candidates to compare, while
    /reroute prioritizes a fast response mid-drive, so they use
    different values here.

    timeout_s differs for the same reason. /reroute happens while
    driving, so it's better to fail after a short wait than make the
    driver wait 30s (on failure, the app just keeps using the original
    route).

    avoid_tolls/avoid_highways — natively supported Valhalla options.
    use_tolls/use_highways take a 0 (avoid as much as possible) to 1
    (don't care) range, but we only need on/off, so true maps to 0.0.
    This doesn't add an extra request — it's folded into the route
    calculation itself, so it has no effect on response speed.
    """
    auto_costing_options = {"maneuver_penalty": MANEUVER_PENALTY}
    if avoid_tolls:
        auto_costing_options["use_tolls"] = 0.0
    if avoid_highways:
        auto_costing_options["use_highways"] = 0.0

    route_request = {
        "locations": [
            {"lat": start_lat, "lon": start_lon},
            {"lat": end_lat,   "lon": end_lon},
        ],
        "costing": "auto",
        "alternates": num_alternates,
        "units": "kilometers",
        "costing_options": {"auto": auto_costing_options},
    }

    try:
        engine_response = requests.post(
            "https://api.stadiamaps.com/route/v1",
            params={"api_key": STADIA_API_KEY},
            json=route_request,
            timeout=timeout_s,
        )
    except Exception:
        raise HTTPException(
            status_code=502,
            detail={"error": "ROUTING_FAILED", "message": "Couldn't connect to the routing engine"}
        )

    if engine_response.status_code != 200:
        raise HTTPException(
            status_code=502,
            detail={"error": "ROUTING_FAILED", "message": "Couldn't fetch a route"}
        )

    engine_data = engine_response.json()

    if "trip" not in engine_data:
        raise HTTPException(
            status_code=404,
            detail={"error": "NO_ROUTE", "message": "No route found"}
        )

    all_trips = [engine_data["trip"]]
    for alt in engine_data.get("alternates", []):
        all_trips.append(alt["trip"])
    return all_trips


# =============================================================
#  NEW — speed limits (Valhalla trace_attributes)
# -------------------------------------------------------------
#  A separate request from /route (route finding). Sending the
#  route's coordinates back gives an actual speed limit (mph) per
#  road segment (edge).
#  Optional feature — adds an extra request that slows things down,
#  so it's only enabled by sending include_speed_limits=true on
#  RouteRequest/RerouteRequest.
# =============================================================

def fetch_speed_limits(coords):
    """Sends coords (route coordinates) to Valhalla trace_attributes to
    get per-segment speed limits (mph). Fails quietly with an empty
    list on error (speed limits are a nice-to-have — the whole route
    request shouldn't fail because of this)."""
    try:
        resp = requests.post(
            "https://api.stadiamaps.com/trace_attributes/v1",
            params={"api_key": STADIA_API_KEY},
            json={
                "shape": [{"lat": lat, "lon": lon} for lat, lon in coords],
                "costing": "auto",
                "shape_match": "map_snap",
                "units": "miles",
            },
            timeout=SPEED_LIMIT_TIMEOUT_S,
        )
        if resp.status_code != 200:
            return []
        return resp.json().get("edges", [])
    except Exception:
        return []


def fill_speed_gaps(speed, source):
    """Fills None (unknown) entries in the speed list using the nearest
    known values on either side.

    Roads typically keep the same speed limit until a new sign appears,
    so it's reasonable to assume a gap continues the nearest known sign's
    speed. If the values on either side of a gap differ, the gap is split
    at its midpoint.

    Both build_speed_index (Valhalla-based) and
    match_speed_limits_locally (local-data-based) reuse this function —
    both face the same underlying problem of "only some segments are
    known, fill in the rest."
    """
    num_points = len(speed)
    known = [i for i, v in enumerate(speed) if v is not None]
    if not known:
        return speed, source

    for i in range(0, known[0]):
        speed[i] = speed[known[0]]
        source[i] = "inferred"
    for i in range(known[-1] + 1, num_points):
        speed[i] = speed[known[-1]]
        source[i] = "inferred"

    for k in range(len(known) - 1):
        i1, i2 = known[k], known[k + 1]
        if i2 - i1 <= 1:
            continue
        v1, v2 = speed[i1], speed[i2]
        mid = (i1 + i2) // 2
        for i in range(i1 + 1, i2):
            speed[i] = v1 if i <= mid else v2
            source[i] = "inferred"

    return speed, source


def build_speed_index(num_points, edges):
    """(Valhalla-based) Assigns a speed limit (mph) to each route point.

    Returns two lists (one entry per route point):
      speed  — mph value (None if unknown)
      source — "osm" (actual data) / "inferred" (estimated) / None (neither)
    """
    speed = [None] * num_points
    for e in edges:
        b = e.get("begin_shape_index", 0)
        en = e.get("end_shape_index", b)
        sl = e.get("speed_limit")
        if sl:
            for i in range(b, min(en + 1, num_points)):
                speed[i] = sl

    source = ["osm" if v is not None else None for v in speed]
    return fill_speed_gaps(speed, source)


# 2026-08-11: per A's request "optimize speed limits the same way lane
# guidance was optimized" — /chill-route used to call Valhalla
# (trace_attributes) live for every route candidate. Switched to the
# same approach as lane guidance (zido_step11): all of LA county's
# maxspeed data is pre-fetched (zido_step12) and loaded into memory at
# server startup, and lookups happen instantly from that in-memory data.
# Falls back automatically to the old live-fetch method if the data
# file is missing.
SPEED_MATCH_RADIUS_M = 30   # use the speed of the nearest road within this distance of a route point
SPEED_SAMPLE_STRIDE = 5     # how many route points to skip between checks
                            # (checking every single point isn't necessary —
                            #  speed limits don't change every few meters, and
                            #  fill_speed_gaps fills in the rest. Also acts as
                            #  a safeguard against slowdowns on long routes)


def match_speed_limits_locally(coords):
    """Looks up, for each route coordinate, the nearest road's speed from
    the pre-fetched speed limit data (SPEED_LIMIT_WAYS). Never calls
    Overpass/Valhalla, so it's fast and reliable.

    Uses the grid index (SPEED_LIMIT_GRID) for lookups — the original
    approach ("crop by route bbox, then search within that") produced
    thousands of candidates on long routes (large bbox), measured up to
    32s in the worst case. Switching to a grid lookup that only checks
    the "~9 nearby cells" per point keeps this consistently fast
    regardless of route length (same lesson as the count_near_route
    memory bug).
    """
    num_points = len(coords)
    speed = [None] * num_points
    source = [None] * num_points

    if num_points == 0:
        return speed, source

    # For each route point (sampled every SPEED_SAMPLE_STRIDE points to
    # avoid checking too densely), pull nearby road candidates from the
    # grid and find the closest one. Remaining points get filled in by
    # fill_speed_gaps.
    for i in range(0, num_points, SPEED_SAMPLE_STRIDE):
        lat, lon = coords[i]
        candidates = query_way_grid(SPEED_LIMIT_GRID, lat, lon)

        best_dist = SPEED_MATCH_RADIUS_M
        best_mph = None
        for way in candidates:
            d = nearest_way_distance_m(lat, lon, way["points"])
            if d < best_dist:
                best_dist = d
                best_mph = way["mph"]
        if best_mph is not None:
            speed[i] = best_mph
            source[i] = "osm"

    return fill_speed_gaps(speed, source)


# =============================================================
#  NEW — highway lane guidance (direct OSM turn:lanes lookup)
# -------------------------------------------------------------
#  Valhalla's /route response has no lane info at all (only exit
#  numbers / sign text). Many roads in raw OSM data do have
#  turn:lanes tags, though, so this fetches and interprets them
#  directly from OSM, same as when road points were collected.
#
#  turn:lanes values separate lanes with "|" (left to right), and
#  within a lane multiple directions are separated by ";". e.g.
#  "left;through|right" → lane 1 = left turn or straight, lane 2 = right turn.
#
#  Also an optional feature (needs include_lane_guidance=true) — has
#  to query the Overpass API each time, which is slow. And it can't
#  always perfectly tell which direction of a bidirectional road
#  applies — most accurate on highway ramps (usually one-way).
# =============================================================

# 2026-08-10 bug fix: B reported lane_guidance always being null for
# "Keep left/Keep right" (type 23/24) instructions. The scoring logic
# (LANE_STRESS_TYPES) already included 18/19/23/24 as "lane-change
# stress", but this mapping (used for the lane guidance lookup) was
# missing them, so these types never even attempted an Overpass lookup
# and always returned None. Added 18 (ramp entry right)/19 (ramp entry
# left) pointing the same direction as 20/21 (exit), and 23 (keep
# right)/24 (keep left) pointing the same direction as 9-11/14-16.
# 25 (merge) was left out — its type alone can't tell direction (could
# merge from either side), and recommending a wrong direction is worse
# than showing nothing — zido's principle is to stay silent when unsure.
LANE_KEYWORDS_FOR_TYPE = {
    14: ["left", "slight_left"], 15: ["left", "slight_left"], 16: ["slight_left", "left"],
    9:  ["slight_right", "right"], 10: ["right", "slight_right"], 11: ["right", "slight_right"],
    18: ["right", "slight_right"], 19: ["left", "slight_left"],
    20: ["right", "slight_right"], 21: ["left", "slight_left"],
    23: ["right", "slight_right"], 24: ["left", "slight_left"],
    8:  ["through", "none"], 22: ["through", "none"],
}


# 2026-08-10: B reported "lane_guidance works sometimes but not other
# times, on the same route." Turned out not to be a data-coverage issue
# but Overpass (free map API) occasionally failing/timing out —
# turn:lanes tags were actually quite plentiful in this area (4,936
# tagged ways confirmed in the same bbox). Two fixes:
#   1) retry once on failure
#   2) turn:lanes tags rarely change, so a static-data cache is used —
#      re-querying the same area within LANE_CACHE_TTL_MINUTES reuses
#      the cache instead of calling Overpass again. Calling it fresh
#      every time was wasteful to begin with, and this also lowers the
#      overall failure rate since re-querying the same area is the most
#      common usage pattern (high cache hit rate).
#   A successful-but-empty result (genuinely no data) is distinguished
#   from a failed request — failures are NOT cached, otherwise a
#   temporary failure would harden into "this area has no data" for 24
#   hours.
#
#   Re-checked on deployment (Render): retrying the same server
#   (overpass-api.de) kept failing — worked fine locally but not on
#   Render, suggesting Overpass is rate-limiting or blocking Render's
#   IP range more aggressively. So instead of "retry the same server,"
#   this now **falls back to a different mirror server**. Overpass
#   officially runs several free mirrors, so if one is blocked another
#   may still work.
OVERPASS_URLS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]

_lane_cache = {}   # {bbox_key: {"fetched_at": timestamp, "ways": [...]}}
LANE_CACHE_TTL_MINUTES = 60 * 24   # 1 day — turn:lanes rarely changes
LANE_CACHE_GRID = 0.02             # rounds to ~2km grid cells to improve cache hit rate

# 2026-08-10: B reported "/chill-route is slow." When an area isn't
# cached and every mirror server (OVERPASS_URLS) also fails, the old
# behavior waited up to 20s (server timeout) × 2 mirrors = 40s worst
# case. Reproducing this showed it can repeat per route candidate (up
# to 3), pushing well past 30s. Also a "nice to have" optional feature,
# so this was cut aggressively to fail fast (20s -> 6s, worst case
# capped at 12s).
OVERPASS_TIMEOUT_S = 6


def fetch_turn_lanes(lat_min, lat_max, lon_min, lon_max):
    """Finds roads with turn:lanes-related tags within the route's bounds.

    If the pre-fetched data (TURN_LANES_WAYS) is available, filters and
    returns from it immediately — fast and reliable, no live Overpass
    calls. If missing (zido_step11 hasn't been run yet), falls back
    automatically to the old live Overpass lookup
    (_fetch_turn_lanes_live).
    """
    if TURN_LANES_WAYS is not None:
        # Instead of scanning all ~67k ways every time, quickly pulls
        # candidates only from the grid cells this bbox overlaps
        # (same reasoning as SPEED_LIMIT_GRID — avoids slowdowns on
        # long routes).
        seen_ids = set()
        nearby = []
        lat = lat_min
        while lat <= lat_max + WAY_GRID_CELL_SIZE:
            lon = lon_min
            while lon <= lon_max + WAY_GRID_CELL_SIZE:
                for way in query_way_grid(TURN_LANES_GRID, lat, lon):
                    wid = id(way)
                    if wid not in seen_ids:
                        seen_ids.add(wid)
                        nearby.append(way)
                lon += WAY_GRID_CELL_SIZE
            lat += WAY_GRID_CELL_SIZE
        return nearby

    return _fetch_turn_lanes_live(lat_min, lat_max, lon_min, lon_max)


def _fetch_turn_lanes_live(lat_min, lat_max, lon_min, lon_max):
    """(Old method, fallback only) Queries Overpass live. Only used when
    the pre-fetched data (data/la_county_turn_lanes.json) is missing."""
    cache_key = (
        round(lat_min / LANE_CACHE_GRID) * LANE_CACHE_GRID,
        round(lat_max / LANE_CACHE_GRID) * LANE_CACHE_GRID,
        round(lon_min / LANE_CACHE_GRID) * LANE_CACHE_GRID,
        round(lon_max / LANE_CACHE_GRID) * LANE_CACHE_GRID,
    )

    cached = _lane_cache.get(cache_key)
    if cached is not None and (time.time() - cached["fetched_at"]) < LANE_CACHE_TTL_MINUTES * 60:
        return cached["ways"]

    margin = 0.01  # buffer (~1km)
    query = f"""
[out:json][timeout:{OVERPASS_TIMEOUT_S}];
(
  way["turn:lanes"]({lat_min - margin},{lon_min - margin},{lat_max + margin},{lon_max + margin});
  way["turn:lanes:forward"]({lat_min - margin},{lon_min - margin},{lat_max + margin},{lon_max + margin});
  way["turn:lanes:backward"]({lat_min - margin},{lon_min - margin},{lat_max + margin},{lon_max + margin});
);
out tags geom;
"""
    elements = None
    for url in OVERPASS_URLS:   # try the next mirror if one is blocked
        try:
            resp = requests.post(
                url,
                data={"data": query},
                headers={"User-Agent": "zido/1.0", "Accept": "application/json"},
                timeout=OVERPASS_TIMEOUT_S + 2,   # slightly more than the server's own timeout
            )
            if resp.status_code == 200:
                elements = resp.json().get("elements", [])
                break
        except Exception:
            pass   # this mirror failed, try the next one

    if elements is None:
        # the request itself failed — don't cache this, just return empty.
        return []

    ways = []
    for e in elements:
        geom = e.get("geometry", [])
        if not geom:
            continue
        ways.append({
            "points": [(pt["lat"], pt["lon"]) for pt in geom],
            "tags": e.get("tags", {}),
        })

    # only cache a successful result (an empty list here means "genuinely no data")
    _lane_cache[cache_key] = {"fetched_at": time.time(), "ways": ways}
    return ways


def parse_lane_string(value):
    """Converts "left;through|right" into [["left","through"], ["right"]]."""
    return [lane.split(";") for lane in value.split("|")]


def nearest_way_distance_m(lat, lon, points):
    """Approximate distance (meters) from (lat, lon) to the nearest point on a way."""
    best = float("inf")
    for plat, plon in points:
        d_lat_m = (plat - lat) * METERS_PER_DEG_LAT
        d_lon_m = (plon - lon) * METERS_PER_DEG_LAT * math.cos(math.radians(lat))
        d = (d_lat_m ** 2 + d_lon_m ** 2) ** 0.5
        if d < best:
            best = d
    return best


def match_lane_guidance(mlat, mlon, mtype, lane_ways, radius_m=30):
    """Finds a turn:lanes road near this maneuver location (mlat, mlon)
    and returns which lane number(s) fit this maneuver (mtype). Returns
    None if nothing matches."""
    keywords = LANE_KEYWORDS_FOR_TYPE.get(mtype)
    if not keywords:
        return None

    best_way = None
    best_dist = radius_m
    for way in lane_ways:
        d = nearest_way_distance_m(mlat, mlon, way["points"])
        if d < best_dist:
            best_dist = d
            best_way = way
    if best_way is None:
        return None

    tags = best_way["tags"]
    # One-way roads (most highway ramps) only have a single turn:lanes
    # value. For two-way roads, one of forward/backward needs to be
    # picked — here, whichever direction has data is preferred (a
    # practical choice rather than perfect direction detection).
    raw = tags.get("turn:lanes") or tags.get("turn:lanes:forward") or tags.get("turn:lanes:backward")
    if not raw:
        return None

    lanes = parse_lane_string(raw)
    recommended = [
        i + 1 for i, lane_opts in enumerate(lanes)
        if any(k in lane_opts for k in keywords)
    ]
    if not recommended:
        return None

    return {"total_lanes": len(lanes), "recommended_lanes": recommended}


def score_trip(trip, fallback_lat, fallback_lon, include_speed=False, include_lanes=False):
    """Converts a single Valhalla trip into zido's response format (one scored route).

    Both /chill-route (multiple candidates) and /reroute (single
    candidate) use this function to score one route. rank is left as 0
    — the caller fills it in if needed (sorted rank when there are
    multiple candidates; always 1 for /reroute).
    """
    coords = []
    maneuvers = []
    for leg in trip["legs"]:
        coords.extend(polyline.decode(leg["shape"], precision=6))
        maneuvers.extend(leg["maneuvers"])

    # optional: speed limits (only when include_speed_limits=true in the request)
    speed_by_index, speed_source_by_index = (None, None)
    if include_speed:
        if SPEED_LIMIT_WAYS is not None:
            speed_by_index, speed_source_by_index = match_speed_limits_locally(coords)
        else:
            edges = fetch_speed_limits(coords)
            speed_by_index, speed_source_by_index = build_speed_index(len(coords), edges)

    # optional: highway lane guidance (only when include_lane_guidance=true in the request)
    lane_ways = []
    if include_lanes:
        lats = [c[0] for c in coords]
        lons = [c[1] for c in coords]
        lane_ways = fetch_turn_lanes(min(lats), max(lats), min(lons), max(lons))

    unprotected_left_count = 0
    protected_left_count   = 0
    uturn_count = 0
    lane_count  = 0
    steps = []

    for m in maneuvers:
        mtype = m.get("type", 0)

        if mtype in UTURN_TYPES:
            uturn_count += 1
        if mtype in LANE_STRESS_TYPES:
            lane_count += 1

        shape_i = m.get("begin_shape_index", 0)
        if shape_i < len(coords):
            location = [coords[shape_i][0], coords[shape_i][1]]
        else:
            location = [fallback_lat, fallback_lon]

        base_text = INSTRUCTIONS.get(mtype, "Continue ahead")
        street_names = m.get("street_names", [])
        if len(street_names) > 0:
            instruction_en = street_names[0] + ": " + base_text
        else:
            instruction_en = base_text

        # for left turns, check whether a signal is within 25m of this
        # point to determine protected vs unprotected. this is the core
        # of the chill score.
        stress_tag = None
        if mtype in LEFT_TURN_TYPES:
            signals_nearby = find_nearby(
                location[0], location[1],
                LEFT_TURN_SIGNAL_RADIUS_M, kinds=["traffic_signal"]
            )
            if len(signals_nearby) > 0:
                protected_left_count += 1
            else:
                unprotected_left_count += 1
                stress_tag = "unprotected_left"
        elif mtype in LANE_STRESS_TYPES:
            stress_tag = "lane_change"
        else:
            # for points that are neither a left turn nor a lane change,
            # also flag it if a stop sign/speed bump is right nearby
            # (show it if present, otherwise stay silent — keeping the
            # "only show what's certain" principle)
            if len(find_nearby(location[0], location[1],
                                STOP_SIGN_RADIUS_M, kinds=["stop_sign"])) > 0:
                stress_tag = "stop_sign"
            elif len(find_nearby(location[0], location[1],
                                  SPEED_BUMP_RADIUS_M, kinds=["speed_bump"])) > 0:
                stress_tag = "speed_bump"

        speed_limit_mph = None
        speed_limit_source = None
        if speed_by_index is not None and shape_i < len(speed_by_index):
            speed_limit_mph = speed_by_index[shape_i]
            speed_limit_source = speed_source_by_index[shape_i]

        lane_guidance = None
        if include_lanes and lane_ways:
            lane_guidance = match_lane_guidance(location[0], location[1], mtype, lane_ways)

        steps.append({
            "instruction_en": instruction_en,
            "distance_m": int(m.get("length", 0) * 1000),
            "time_s": int(m.get("time", 0)),
            "maneuver_type": mtype,
            "location": location,
            "stress_tag": stress_tag,
            "speed_limit_mph": speed_limit_mph,
            "speed_limit_source": speed_limit_source,
            "lane_guidance": lane_guidance,
        })

    # stop signs/speed bumps need to be counted anywhere along the whole
    # route (including plain straight segments), not just at turn
    # points, so count_near_route scans the full route separately (this
    # is a distinct calculation from the stress_tag marking in the steps
    # loop above).
    stop_sign_count  = count_near_route(coords, STOP_SIGN_RADIUS_M, ["stop_sign"])
    speed_bump_count = count_near_route(coords, SPEED_BUMP_RADIUS_M, ["speed_bump"])

    total_time_s = trip["summary"]["time"]
    total_min = total_time_s / 60

    score = (
        unprotected_left_count * W_UNPROTECTED_LEFT
        + protected_left_count  * W_PROTECTED_LEFT
        + uturn_count * W_UTURN
        + lane_count  * W_LANE
        + stop_sign_count  * W_STOP_SIGN
        + speed_bump_count * W_SPEED_BUMP
        + total_min   * W_PER_MIN
    )

    return {
        "rank": 0,
        "fatigue_score": round(score, 1),
        "total_distance_m": int(trip["summary"]["length"] * 1000),
        "total_time_s": int(total_time_s),
        # v0.5.1 — whether this route actually passes through tolls/highways.
        # always included regardless of avoid_tolls/avoid_highways — useful as reference info.
        "has_toll": bool(trip["summary"].get("has_toll", False)),
        "has_highway": bool(trip["summary"].get("has_highway", False)),
        "counts": {
            "unprotected_left": unprotected_left_count,
            "lane_change": lane_count,
            "stop_sign": stop_sign_count,
            "speed_bump": speed_bump_count,
            "protected_left": protected_left_count,
            "total_left_turn": unprotected_left_count + protected_left_count,
            "uturn": uturn_count,
        },
        "geometry": [[c[0], c[1]] for c in coords],
        "steps": steps,
    }


@app.post("/chill-route")
def chill_route(req: RouteRequest):

    # --- 5-1. validate coordinates ---
    if len(req.start) != 2 or len(req.end) != 2:
        raise HTTPException(
            status_code=400,
            detail={"error": "BAD_INPUT", "message": "Coordinates must be [lat, lon] pairs"}
        )

    start_lat, start_lon = req.start
    end_lat, end_lon = req.end
    check_in_la_or_raise(start_lat, start_lon)
    check_in_la_or_raise(end_lat, end_lon)

    # --- 5-2. request candidates from the routing engine ---
    all_trips = request_trips(
        start_lat, start_lon, end_lat, end_lon, NUM_ALTERNATIVES,
        avoid_tolls=req.avoid_tolls, avoid_highways=req.avoid_highways,
    )

    # --- 5-3. score each route ---
    scored_routes = [
        score_trip(
            trip, start_lat, start_lon,
            include_speed=req.include_speed_limits,
            include_lanes=req.include_lane_guidance,
        )
        for trip in all_trips
    ]

    # --- 5-4. sort and assign rank ---
    scored_routes.sort(key=lambda r: r["fatigue_score"])

    for i in range(len(scored_routes)):
        scored_routes[i]["rank"] = i + 1

    return {"routes": scored_routes}


# =============================================================
#  [6] NEW — fast recompute when going off-route
# -------------------------------------------------------------
#  Used when the driver goes off the guided route mid-drive.
#  The only difference from /chill-route is fetching just 1
#  candidate instead of 3 (num_alternates=0). While driving,
#  getting a fast new route from the current position matters
#  much more than comparing multiple options.
# =============================================================

REROUTE_ALTERNATES = 0    # single candidate, no comparison — speed first
REROUTE_TIMEOUT_S   = 12  # can't wait 30s while driving. try briefly and
                          # fail fast — the app can just keep the original route.


class RerouteRequest(BaseModel):
    current: list[float]   # current location [lat, lon]
    end: list[float]       # original destination stays the same [lat, lon]
    include_speed_limits: bool = False
    include_lane_guidance: bool = False
    avoid_tolls: bool = False
    avoid_highways: bool = False


@app.post("/reroute")
def reroute(req: RerouteRequest):

    if len(req.current) != 2 or len(req.end) != 2:
        raise HTTPException(
            status_code=400,
            detail={"error": "BAD_INPUT", "message": "Coordinates must be [lat, lon] pairs"}
        )

    cur_lat, cur_lon = req.current
    end_lat, end_lon = req.end
    check_in_la_or_raise(cur_lat, cur_lon)
    check_in_la_or_raise(end_lat, end_lon)

    trips = request_trips(
        cur_lat, cur_lon, end_lat, end_lon,
        REROUTE_ALTERNATES, timeout_s=REROUTE_TIMEOUT_S,
        avoid_tolls=req.avoid_tolls, avoid_highways=req.avoid_highways,
    )

    route = score_trip(
        trips[0], cur_lat, cur_lon,
        include_speed=req.include_speed_limits,
        include_lanes=req.include_lane_guidance,
    )
    route["rank"] = 1

    return {"route": route}
