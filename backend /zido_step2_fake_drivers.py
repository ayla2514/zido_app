# One-line summary: simulates 3 fake drivers continuously moving along preset LA routes
# (and occasionally posting road reports), so the map has live data to test against.

# =============================================================
#  zido - [Step 2] Fake driver simulator
# -------------------------------------------------------------
#  Location: C:\zido\backend\zido_step2_fake_drivers.py
#
#  What this does:
#   Keeps 3 fake drivers continuously moving around LA roads.
#   Occasionally posts road condition reports too.
#
#  Why this is needed:
#   Without moving data on the map, it's hard to tell whether
#   the map code is broken or there's just no data to show.
#   Running this makes pins actually move on the map so that's
#   easy to distinguish.
#
#  Usage: run it → leave it running → Ctrl+C to stop
# =============================================================

import os
import time                                   # for waiting
import math                                   # for heading calculation
import random                                 # for random reports
from pathlib import Path
from datetime import datetime, timezone       # for building the current timestamp
from dotenv import load_dotenv
from supabase import create_client


# =============================================================
#  [Config] ← change values here if needed
# =============================================================

# .env is one folder above this file, at C:\zido
env_path = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(dotenv_path=env_path)

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

# how often (seconds) to update position
UPDATE_INTERVAL_SEC = 5

# how many small steps to split each segment between two points into
# higher = slower, smoother movement
STEPS_PER_SEGMENT = 20

# chance a report gets posted on any given update (0.05 = 5%)
REPORT_CHANCE = 0.05

# the 3 fake drivers' appearance
DRIVER_NAMES  = ["Test_MintCar",   "Test_PeachCat", "Test_LavenderBear"]
DRIVER_SHAPES = ["car",            "cat",           "bear"]
DRIVER_COLORS = ["mint",           "peach",         "lavender"]

# report kinds (only values the database allows)
REPORT_KINDS = ["accident", "construction", "jam", "police", "pothole", "object", "hazard"]


# =============================================================
#  [0] Check keys
# =============================================================
print("=" * 55)
print("zido - fake driver simulator")
print("=" * 55)

if SUPABASE_URL is None or SUPABASE_KEY is None:
    print("\n❌ Couldn't read the keys from .env.")
    print("   Looked in:", env_path)
    raise SystemExit()

print("✅ Keys OK\n")


# =============================================================
#  [1] Routes the fake drivers will follow
# -------------------------------------------------------------
#  Each route is a list of (lat, lon) points. The program connects
#  these points automatically for smooth movement between them.
#  After reaching the last point, it loops back to the start.
#
#  All coordinates must be within LA bounds:
#     lat 33.70~34.35 / lon -118.70~-118.10
#     the database rejects anything outside this range.
# =============================================================

ROUTES = [
    # driver 1: downtown LA loop
    [
        (34.0522, -118.2437),
        (34.0407, -118.2468),
        (34.0430, -118.2673),
        (34.0600, -118.2600),
        (34.0620, -118.2450),
    ],
    # driver 2: I-405 north-south back and forth
    [
        (34.0300, -118.4400),
        (34.0600, -118.4450),
        (34.1000, -118.4700),
        (34.1500, -118.4800),
        (34.1000, -118.4700),
        (34.0600, -118.4450),
    ],
    # driver 3: Santa Monica coastline
    [
        (34.0100, -118.4960),
        (34.0250, -118.5100),
        (34.0180, -118.5200),
        (33.9950, -118.4750),
    ],
]


# =============================================================
#  [2] Break each route into small steps
# -------------------------------------------------------------
#  Using just the points above would make movement jump around.
#  Each segment between two points is split into STEPS_PER_SEGMENT
#  pieces ahead of time, producing a dense list of coordinates
#  that moves smoothly.
# =============================================================
print("🗺️  Splitting routes into small steps...")

dense_routes = []   # holds the densified routes

for route in ROUTES:
    dense_points = []

    for i in range(len(route)):
        lat1, lon1 = route[i]
        # after the last point, loop back to the first (a cycle)
        lat2, lon2 = route[(i + 1) % len(route)]

        for step in range(STEPS_PER_SEGMENT):
            # ratio is between 0.0 and 1.0.
            # 0 = start point, closer to 1 = closer to the next point.
            ratio = step / STEPS_PER_SEGMENT
            mid_lat = lat1 + (lat2 - lat1) * ratio
            mid_lon = lon1 + (lon2 - lon1) * ratio
            dense_points.append((mid_lat, mid_lon))

    dense_routes.append(dense_points)
    print(f"   route {len(dense_routes)}: {len(dense_points)} points")


# =============================================================
#  [3] Sign in the 3 fake drivers
# -------------------------------------------------------------
#  Important: each driver needs its own separate client/connection,
#  since only one user can be signed in per connection.
# =============================================================
print("\n🔑 Signing in fake drivers...")

driver_clients = []   # one connection per driver
driver_ids = []       # one user id per driver

for i in range(len(DRIVER_NAMES)):
    # open a fresh connection just for this driver
    client = create_client(SUPABASE_URL, SUPABASE_KEY)

    # anonymous sign-in
    auth_result = client.auth.sign_in_anonymously()
    user_id = auth_result.user.id

    # create the profile
    client.table("profiles").insert({
        "id": user_id,
        "nickname": DRIVER_NAMES[i],
        "pin_shape": DRIVER_SHAPES[i],
        "pin_color": DRIVER_COLORS[i],
    }).execute()

    driver_clients.append(client)
    driver_ids.append(user_id)

    print(f"   ✅ {DRIVER_NAMES[i]} ready")


# =============================================================
#  [4] Keep moving (main loop)
# -------------------------------------------------------------
#  Loops forever until Ctrl+C is pressed.
# =============================================================
print("\n" + "=" * 55)
print("🚗 Driving started! (Ctrl+C to stop)")
print("=" * 55 + "\n")

# tracks which point index each driver is currently at on their route
current_index = [0, 0, 0]

# counts how many updates have happened
tick_count = 0

try:
    while True:
        tick_count += 1

        # build the current time in a format the database understands.
        # sending the literal string "now()" would error, so it's
        # built directly in Python instead.
        now_iso = datetime.now(timezone.utc).isoformat()

        # --- process each driver ---
        for i in range(len(driver_clients)):

            points = dense_routes[i]
            index = current_index[i]

            # current position
            lat, lon = points[index]

            # next position (needed to compute heading and speed)
            next_index = (index + 1) % len(points)
            next_lat, next_lon = points[next_index]

            # --- compute heading (0-360 degrees, 0 = north) ---
            # atan2 gives the angle between two points.
            delta_lat = next_lat - lat
            delta_lon = next_lon - lon
            heading = math.degrees(math.atan2(delta_lon, delta_lat)) % 360

            # --- compute speed (meters per second) ---
            # 1 degree of latitude is ~111000m, and at LA's latitude
            # 1 degree of longitude is ~92000m.
            meters_lat = delta_lat * 111000
            meters_lon = delta_lon * 92000
            distance_m = (meters_lat ** 2 + meters_lon ** 2) ** 0.5
            speed_mps = distance_m / UPDATE_INTERVAL_SEC

            # --- save the position to the database ---
            # insert means "add a new row" — not overwriting, appending.
            # positions need to stack up like this so a "position from
            # 1 minute ago" can be picked out later.
            # (overwriting would leave only the latest position, with
            # nothing from a minute ago)
            driver_clients[i].table("live_positions").insert({
                "user_id": driver_ids[i],
                "lat": lat,
                "lon": lon,
                "heading": heading,
                "speed_mps": speed_mps,
                "is_moving": True,      # fake drivers are always "driving"
                "recorded_at": now_iso,
            }).execute()

            # advance one step to the next point
            current_index[i] = next_index

        # --- occasionally post a report ---
        if random.random() < REPORT_CHANCE:
            who = random.randint(0, len(driver_clients) - 1)
            report_lat, report_lon = dense_routes[who][current_index[who]]
            report_kind = random.choice(REPORT_KINDS)

            driver_clients[who].table("reports").insert({
                "user_id": driver_ids[who],
                "kind": report_kind,
                "lat": report_lat,
                "lon": report_lon,
            }).execute()

            print(f"   🚨 {DRIVER_NAMES[who]} posted a '{report_kind}' report")

        # --- show progress (only every 10 ticks) ---
        if tick_count % 10 == 0:
            print(f"[update #{tick_count}] all 3 drivers still driving...")

        # wait until the next update
        time.sleep(UPDATE_INTERVAL_SEC)


# =============================================================
#  [5] Cleanup when stopped with Ctrl+C
# -------------------------------------------------------------
#  Setting is_moving to False would only hide them from the
#  nearby_drivers view eventually — deleting the position rows
#  outright removes them immediately, since stopped cars shouldn't
#  linger on the map.
# =============================================================
except KeyboardInterrupt:
    print("\n\n🛑 Stopping...")

    for i in range(len(driver_clients)):
        # delete all my position records.
        # a driver should disappear from the map immediately once
        # they stop driving (safety rule 4: disappear when stopped —
        # the key safeguard against a parked location revealing
        # someone's home address).
        driver_clients[i].table("live_positions").delete().eq(
            "user_id", driver_ids[i]
        ).execute()

    print("✅ Fake drivers removed from the map.")
    print("\nTo delete the test profiles, run this in the SQL Editor:")
    print("  delete from profiles where nickname like 'Test_%';")
