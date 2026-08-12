# One-line summary: fetches LA city's official public (city-owned) parking lot inventory
# (location, pricing, hours, spaces) as GeoJSON and saves it to a CSV, covering City of LA only.

# ============================================================
# zido - [Step 12] Public parking lot collection
# zido_step12_collect_public_parking_lots.py
# ------------------------------------------------------------
# Collects public parking lot (city-run lots/structures) locations + pricing.
#
# Why this exists: A asked for public parking lots (structures/lots),
# not just metered street parking, to also be turned into price +
# location data. Investigating found the City of LA publishes every
# lot it runs under "City-Owned Parking Lots" as map data (GeoJSON) —
# including hourly/daily/monthly rates, space count, and operating hours.
# Same reasoning as the metered parking data — this also only covers
# "City of LA" scope (not the whole county).
#
# Source: LA GeoHub - City Owned Parking Lots (LADOT NavigateLA data)
#       https://hub.arcgis.com/api/download/v1/items/be7c8c4ab95b4d82af18255ad1a3212c/geojson?layers=2
# ============================================================
import requests
import csv
# ---------- only change values here ----------
GEOJSON_URL = "https://hub.arcgis.com/api/download/v1/items/be7c8c4ab95b4d82af18255ad1a3212c/geojson"
OUT_CSV = r"C:\zido\data\public_parking_lots\la_city_public_parking_lots.csv"
# --------------------------------------
print("=" * 60)
print("zido - City of LA public parking lot collection")
print("=" * 60)
resp = requests.get(GEOJSON_URL, params={"redirect": "true", "layers": "2"}, timeout=60)
resp.raise_for_status()
features = resp.json()["features"]
print(f"Lots received: {len(features)}")
written = 0
skipped = 0
with open(OUT_CSV, "w", encoding="utf-8-sig", newline="") as f:
    writer = csv.writer(f)
    writer.writerow([
        "facility_id", "lot_name", "address", "city", "zipcode",
        "operator", "hours", "hourly_cost", "daily_cost", "monthly_cost",
        "spaces", "status", "lat", "lon",
    ])
    for feat in features:
        p = feat["properties"]
        lat, lon = p.get("Lat"), p.get("Lon")
        if lat is None or lon is None:
            skipped += 1
            continue
        writer.writerow([
            p.get("FacilityID", ""),
            p.get("LotName", ""),
            p.get("Address", ""),
            p.get("City", ""),
            p.get("Zipcode", ""),
            p.get("Operator", ""),
            p.get("Hours", ""),
            p.get("HourlyCost", ""),
            p.get("DailyCost", ""),
            p.get("MonthlyCost", ""),
            p.get("Spaces", ""),
            p.get("Status", ""),
            lat, lon,
        ])
        written += 1
print(f"Saved: {OUT_CSV} ({written} rows, {skipped} skipped for missing coordinates)")
print("\nNote: this data also only covers 'City of LA' proper.")
print("Some entries may have a Status other than 'Operational' (closed/private etc.) — left as-is in the data.")
