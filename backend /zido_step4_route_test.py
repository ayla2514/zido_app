# One-line summary: standalone test script that asks the routing engine (Valhalla via
# Stadia Maps) for multiple route candidates and ranks them by how many left turns/U-turns each has —
# the v0 prototype of the "chill route" scoring logic.

# =============================================================
#  zido - [Step 4] Route engine connection test
# -------------------------------------------------------------
#  Location: C:\zido\backend\zido_step4_route_test.py
#
#  What this does:
#   1) asks Stadia Maps (Valhalla) for several "A to B" route candidates
#   2) counts how many left turns each route has
#   3) shows them sorted from fewest left turns to most
#
#  Why this matters:
#   Even with zero OSM data, left turns can be counted just from the
#   "maneuver type" the routing engine already provides.
#   This is v0 (the simplest version) of Easy Drive.
#
#  One-time setup:
#     pip install requests python-dotenv
# =============================================================

import os
import requests                    # for sending HTTP requests
from pathlib import Path
from dotenv import load_dotenv


# =============================================================
#  [Config] ← only change values here
# =============================================================

env_path = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(dotenv_path=env_path)

STADIA_API_KEY = os.getenv("STADIA_API_KEY")

# start point (lat, lon) — example: downtown LA
START_LAT = 34.0522
START_LON = -118.2437

# end point (lat, lon) — example: Santa Monica
END_LAT = 34.0195
END_LON = -118.4912

# how many route candidates to request (more = better chance of finding
# a good route, but slower)
NUM_ALTERNATIVES = 3

# how much to penalize turns (higher = prefers simpler routes with fewer turns)
MANEUVER_PENALTY = 30

# LA county bounds
LA_LAT_MIN, LA_LAT_MAX = 33.69, 34.83
LA_LON_MIN, LA_LON_MAX = -118.96, -117.64


# =============================================================
#  [Reference] Valhalla's maneuver type codes
# -------------------------------------------------------------
#  The routing engine tags each instruction with a numeric type.
#  Only the ones we care about are listed here.
#
#   9 slight right   10 right   11 sharp right
#  14 sharp left      15 left    16 slight left    ← left-turn group, want to avoid
#  12 u-turn(right)   13 u-turn(left)               ← most to avoid
#  18/19 ramp entry  20/21 exit  23/24 keep lane  25 merge  ← lane-change stress
# =============================================================

LEFT_TURN_TYPES  = [14, 15, 16]                   # left-turn group
UTURN_TYPES      = [12, 13]                       # u-turns
RIGHT_TURN_TYPES = [9, 10, 11]                    # right turns (for reference)
LANE_STRESS_TYPES = [18, 19, 20, 21, 23, 24, 25]  # ramps/exits/merges

# v0 scoring weights (will get more precise once signals/stop signs are added)
W_LEFT_TURN = 15    # left turn (protected vs unprotected not distinguished yet)
W_UTURN     = 40    # u-turn
W_LANE      = 25    # lane change / merge
W_PER_MIN   = 2     # per minute (also factors in efficiency)


# =============================================================
#  [0] Check keys and coordinates
# =============================================================
print("=" * 60)
print("zido - route engine connection test")
print("=" * 60)

if STADIA_API_KEY is None:
    print("\n❌ Couldn't read STADIA_API_KEY from .env.")
    print("   Check that this line exists in C:\\zido\\.env:")
    print("   STADIA_API_KEY=your_key_here")
    raise SystemExit()

print("✅ Key found:", STADIA_API_KEY[:10] + "...")

# check both points are within LA county
start_ok = (LA_LAT_MIN <= START_LAT <= LA_LAT_MAX) and (LA_LON_MIN <= START_LON <= LA_LON_MAX)
end_ok   = (LA_LAT_MIN <= END_LAT   <= LA_LAT_MAX) and (LA_LON_MIN <= END_LON   <= LA_LON_MAX)

if not (start_ok and end_ok):
    print("\n❌ Start or end point is outside LA county.")
    raise SystemExit()

print("✅ Both start and end points are within LA county\n")


# =============================================================
#  [1] Request routes from the routing engine
# -------------------------------------------------------------
#  "costing": "auto" means car mode.
#  "alternates" means how many candidates to ask for.
#  raising "maneuver_penalty" makes it prefer simpler routes with fewer turns.
# =============================================================
print("🚗 Requesting route candidates...")

route_request = {
    "locations": [
        {"lat": START_LAT, "lon": START_LON},
        {"lat": END_LAT,   "lon": END_LON},
    ],
    "costing": "auto",
    "alternates": NUM_ALTERNATIVES,
    "units": "kilometers",
    "costing_options": {
        "auto": {
            "maneuver_penalty": MANEUVER_PENALTY
        }
    },
}

response = requests.post(
    "https://api.stadiamaps.com/route/v1",
    params={"api_key": STADIA_API_KEY},
    json=route_request,
    timeout=30,
)

# stop here and show why if this failed
if response.status_code != 200:
    print("\n❌ Route request failed")
    print("   Status code:", response.status_code)
    print("   Body:", response.text[:300])
    print("\n   401 means → the key is wrong")
    print("   429 means → the free usage quota was exceeded")
    raise SystemExit()

route_data = response.json()

# Valhalla puts the first route under 'trip' and the rest under
# 'alternates'. Merge them into one list to handle them the same way.
all_trips = [route_data["trip"]]
for alt in route_data.get("alternates", []):
    all_trips.append(alt["trip"])

print(f"✅ Got {len(all_trips)} route candidate(s)\n")


# =============================================================
#  [2] Count left turns for each route
# =============================================================
results = []   # holds the results

for trip_index in range(len(all_trips)):
    trip = all_trips[trip_index]

    # gather all maneuvers for this route into one list
    maneuvers = []
    for leg in trip["legs"]:
        maneuvers.extend(leg["maneuvers"])

    # count by type
    left_count  = 0
    uturn_count = 0
    right_count = 0
    lane_count  = 0

    for m in maneuvers:
        mtype = m.get("type", 0)

        if mtype in LEFT_TURN_TYPES:
            left_count += 1
        if mtype in UTURN_TYPES:
            uturn_count += 1
        if mtype in RIGHT_TURN_TYPES:
            right_count += 1
        if mtype in LANE_STRESS_TYPES:
            lane_count += 1

    # time and distance
    total_min = trip["summary"]["time"] / 60
    total_km  = trip["summary"]["length"]

    # v0 score calculation
    score = (
        left_count  * W_LEFT_TURN
        + uturn_count * W_UTURN
        + lane_count  * W_LANE
        + total_min   * W_PER_MIN
    )

    results.append({
        "index": trip_index + 1,
        "score": round(score, 1),
        "time_min": round(total_min, 1),
        "distance_km": round(total_km, 1),
        "left_turns": left_count,
        "uturns": uturn_count,
        "right_turns": right_count,
        "lane_changes": lane_count,
        "maneuvers": maneuvers,
    })


# =============================================================
#  [3] Show the results
# =============================================================
print("=" * 60)
print("Route comparison (lower score = more comfortable route)")
print("=" * 60)
print()
print(f"{'#':<5}{'score':>8}{'time':>8}{'dist':>9}{'left':>8}{'uturn':>6}{'right':>8}{'lane':>7}")
print("-" * 60)

# sort by score, lowest first
results_sorted = sorted(results, key=lambda r: r["score"])

for r in results_sorted:
    print(f"{r['index']:<5}{r['score']:>8}{r['time_min']:>7}min{r['distance_km']:>8}km"
          f"{r['left_turns']:>8}{r['uturns']:>6}{r['right_turns']:>8}{r['lane_changes']:>7}")

print("-" * 60)


# most chill route vs fastest route
best = results_sorted[0]
fastest = sorted(results, key=lambda r: r["time_min"])[0]

print()
print(f"😌 Most chill : route {best['index']} — {best['left_turns']} left turns, {best['time_min']}min")
print(f"⚡ Fastest    : route {fastest['index']} — {fastest['left_turns']} left turns, {fastest['time_min']}min")

time_cost = best["time_min"] - fastest["time_min"]
left_saved = fastest["left_turns"] - best["left_turns"]

print()
if best["index"] == fastest["index"]:
    print("→ The most comfortable route is also the fastest! Perfect 🎉")
else:
    print(f"→ Takes {round(time_cost, 1)} more minutes, but {left_saved} fewer left turns 🍃")


# =============================================================
#  [4] Look at the most chill route's turn-by-turn instructions
# -------------------------------------------------------------
#  Seeing what the routing engine actually returns makes the next
#  stage (the scoring engine) much easier to understand.
# =============================================================
print()
print("=" * 60)
print(f"Route {best['index']} instructions (first 12 only)")
print("=" * 60)

count = 0
for m in best["maneuvers"]:
    if count >= 12:
        break

    mtype = m.get("type", 0)

    # mark left turns
    mark = ""
    if mtype in LEFT_TURN_TYPES:
        mark = "  ← left turn"
    if mtype in UTURN_TYPES:
        mark = "  ← u-turn"

    instruction = m.get("instruction", "")
    distance_m = int(m.get("length", 0) * 1000)

    print(f"  [{mtype:>2}] {instruction} ({distance_m}m){mark}")
    count += 1

print()
print("Note: the number in brackets is the 'maneuver type'.")
print("  Left turns are all treated the same for now, but once")
print("  the next stage adds signal data, 'protected left turn'")
print("  and 'unprotected left turn' can be told apart.")
