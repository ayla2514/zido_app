# zido

iOS navigation app that routes drivers along calmer, less-tiring paths (SwiftUI + MapKit), for the Los Angeles area. Backed by a FastAPI service for road/routing data and a Supabase database for the driver-community layer (live positions, reports, reactions, blocking).

## Structure

```
backend /   FastAPI server + one-off data collection scripts
sql/        Supabase schema and incremental fixes (apply in order)
data/       Collected/crowdsourced datasets, grouped by topic
```

## Backend (`backend /`)

- **`zido_server.py`** — FastAPI server. Serves road-point data (signals, stop signs, speed bumps), live road closures/alerts, air quality, nearby free parking, and computes "chill" (low-fatigue) driving routes.
  - `GET /road-points` — signals/stop signs/bumps/POIs near a point or route
  - `GET /road-closures`, `GET /road-alerts` — live Caltrans closures/alerts
  - `GET /air-quality` — air quality near a point
  - `GET /parking-near` — nearby free parking, including time-restricted zones
  - `POST /chill-route`, `POST /reroute` — routes ranked by fatigue (fewer left turns/U-turns, etc.) via Valhalla/Stadia Maps
  - `GET /health`, `GET /`

- **Data collection scripts** (`zido_stepN_*.py`) — one-off scripts that pre-fetch data from OSM/LADOT/Caltrans so the server doesn't have to hit those sources live:
  - `step1_connection` — Supabase connectivity smoke test
  - `step2_fake_drivers` — simulates fake drivers for local testing
  - `step3_security_check` — attacks the app's own RLS policies as an outside user, to confirm they hold
  - `step4_route_test` — prototype of the chill-route scoring logic
  - `step5_collect_weho` — pilot OSM pull (signals/stop signs/bumps) for West Hollywood
  - `step6_collect_la_county` — same, for all of LA county
  - `step7_collect_poi` — gas stations + free-tagged parking lots
  - `step8_collect_street_sweeping` — street sweeping schedule, geocoded into map zones
  - `step9_collect_turn_lanes` — turn-lane tags, pre-fetched from Overpass
  - `step10_collect_speed_limits` — OSM maxspeed tags, pre-fetched from Overpass
  - `step11_collect_metered_parking` — LADOT metered parking inventory
  - `step12_collect_public_parking_lots` — LA city public parking lot inventory

## Database (`sql/`)

Apply in order in the Supabase SQL Editor: `zido_schema.sql` → `zido_fix01_position_stacking` → `zido_fix02_la_county` → `zido_fix03_report_vote_counts.sql` → `zido_fix04_cleanup_schedule.sql` → `zido_fix05_block_filtering.sql` → `zido_fix06_gas_price_reports.sql` → `zido_fix07_sign_photo_uploads.sql` → `zido_fix08_hotspots.sql`.

| File | What it does |
|---|---|
| `zido_schema.sql` | Base schema: profiles, delayed/blurred live positions, road reports, reactions, blocking, RLS policies, realtime, cleanup |
| `zido_fix01_position_stacking` | Switches `live_positions` from overwrite to append, so the "1 minute ago" view actually has data |
| `zido_fix02_la_county` | Widens the service-area check from LA city to LA county |
| `zido_fix03_report_vote_counts.sql` | Trigger that auto-tallies report votes and auto-hides a report once downvotes outnumber upvotes by 3+ |
| `zido_fix04_cleanup_schedule.sql` | Schedules `cleanup_old_data()` via pg_cron (every 10 min), in SQL instead of a manual dashboard setting |
| `zido_fix05_block_filtering.sql` | Enforces mutual (two-way) blocking at the DB level for reports, waves, and live positions |
| `zido_fix06_gas_price_reports.sql` | Adds a `cheap_gas` report kind with a price field, crowdsourcing gas prices |
| `zido_fix07_sign_photo_uploads.sql` | Table + storage bucket for in-app sign photo uploads (street parking data collection) |
| `zido_fix08_hotspots.sql` | Table for curated map hotspots (cafes/food/trendy spots), container ready to populate |

Core safety rules baked into the schema:
- All coordinates are checked against LA bounds at the DB level (`is_in_la`), not just in app code.
- Other users' live positions are only ever exposed through `nearby_drivers`, which delays (60s+), blurs (~100m), and filters to moving-only — never the raw table.
- No real names/emails or free-text/photos in reports; no 1:1 messaging — only broadcast reactions.

## Data (`data/`)

Collected datasets, one subfolder per topic, each holding its CSV/JSON:

```
data/
├── metered_parking/       LADOT metered parking inventory
├── public_parking_lots/   LA city public parking lots
└── street_parking/        Road points, POI road points, speed limits, turn lanes, street sweeping zones
```
