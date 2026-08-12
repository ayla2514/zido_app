# One-line summary: fetches LA city's official metered parking inventory (location, price,
# time limit) from LADOT's open data and saves it to a CSV, covering City of LA only.

# ============================================================
# zido - [Step 11] Metered parking collection
# zido_step11_collect_metered_parking.py
# ------------------------------------------------------------
# Collects paid metered parking space locations + pricing.
#
# Why this exists: A asked about "locations of paid meter parking
# spots," and investigating found that the City of LA (LADOT) freely
# publishes all 34,950 city meters (location + price + time limit)
# with no key required (data.lacity.org). No dataset covering all of
# LA county could be found (Santa Monica was mentioned as having an
# API, but it wasn't actually reachable; other cities have no public
# data at all for this) -> so the "City of LA only" scope is honestly
# reflected in this file's name/data.
#
# Source: LADOT Metered Parking Inventory & Policies
#       https://data.lacity.org/resource/s49e-q6j2.json (Socrata SODA API)
# ============================================================
import requests
import csv
# ---------- only change values here ----------
API_URL = "https://data.lacity.org/resource/s49e-q6j2.json"
PAGE_SIZE = 5000  # how many rows to fetch per page (kept safely under Socrata's default limit)
OUT_CSV = r"C:\zido\data\metered_parking\la_city_metered_parking.csv"
# --------------------------------------
print("=" * 60)
print("zido - City of LA metered parking collection")
print("=" * 60)
all_rows = []
offset = 0
# the Socrata API doesn't return everything at once, it pages the
# results -> keep increasing offset until no more data comes back
while True:
    params = {
        "$limit": PAGE_SIZE,
        "$offset": offset,
        "$select": "spaceid,blockface,metertype,ratetype,raterange,timelimit,latlng",
    }
    resp = requests.get(API_URL, params=params, timeout=60)
    resp.raise_for_status()
    page = resp.json()
    if not page:
        break  # no more data, done
    all_rows.extend(page)
    print(f"  got {offset} ~ {offset + len(page)}")
    offset += PAGE_SIZE
    if len(page) < PAGE_SIZE:
        break  # this was the last page
print(f"\nGot {len(all_rows)} meters total")
# latlng comes nested as {"latitude": "..", "longitude": ".."}, so
# rows missing lat/lon (a few coordinates are occasionally missing) are skipped
written = 0
skipped = 0
with open(OUT_CSV, "w", encoding="utf-8-sig", newline="") as f:
    writer = csv.writer(f)
    writer.writerow(["spaceid", "blockface", "metertype", "ratetype", "raterange", "timelimit", "lat", "lon"])
    for row in all_rows:
        latlng = row.get("latlng")
        if not latlng or "latitude" not in latlng or "longitude" not in latlng:
            skipped += 1
            continue
        writer.writerow([
            row.get("spaceid", ""),
            row.get("blockface", ""),
            row.get("metertype", ""),
            row.get("ratetype", ""),
            row.get("raterange", ""),
            row.get("timelimit", ""),
            latlng["latitude"],
            latlng["longitude"],
        ])
        written += 1
print(f"Saved: {OUT_CSV} ({written} rows, {skipped} skipped for missing coordinates)")
print("\nNote: this data only covers 'City of LA' proper.")
print("West Hollywood/Beverly Hills/Santa Monica etc. are not included (their own systems, no public data).")
