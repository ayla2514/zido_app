-- One-line summary: adds a 'cheap_gas' report kind with a price field, reusing the existing crowd-reporting (reports) structure to crowdsource gas prices since no free real-time price data exists.

-- =============================================================
--  zido fix 06 — gas price reports (add cheap_gas report kind)
-- -------------------------------------------------------------
--  Why:
--   There's no free source of real-time gas prices (not in OSM,
--   and every real-time price API is paid). So instead, this
--   reuses the existing "road condition reports" (reports)
--   structure to let people report "gas is cheap here" themselves —
--   this is actually how Waze's gas price info works too (crowd-
--   sourced reports).
--
--  Fix:
--   1) allows 'cheap_gas' as an additional value in reports.kind.
--   2) adds a price_usd_per_gallon column to reports.
--      Any report that isn't cheap_gas (accident, construction,
--      etc.) must always have this as null (a safeguard so price
--      data doesn't leak into unrelated report kinds).
--   3) no free text is used (zido rule — no photos or free-text in
--      reports). Something like "really cheap" can just be a fixed
--      phrase the app shows whenever kind='cheap_gas'. The server
--      only ever receives a number (the price).
--
--  Voting (still/gone) is reused as-is (already automated in fix03):
--   for a cheap_gas report, "still" means "this price is still
--   accurate" and "gone" means "price changed/wrong" — the app just
--   needs to show different wording for this kind. Once gone votes
--   outnumber still by 3+, the fix03 trigger auto-hides it — so
--   stale price info naturally gets filtered out by other users.
--
--  Where the price's location comes from:
--   The app knows this, not the server (zido_server.py). When the
--   app shows a gas station fetched via /road-points kinds=fuel and
--   the user taps it, the app just submits that same coordinate as
--   reports.lat/lon. So no server code needs to change at all (same
--   as every other community feature — the app calls Supabase directly).
--
--  Usage:
--   Supabase > SQL Editor > New query > paste all > RUN
--   (apply order: schema -> fix01 -> fix02 -> fix03 -> fix04 -> fix05 -> fix06)
-- =============================================================

-- [1] add 'cheap_gas' to allowed kind values
alter table reports drop constraint if exists reports_kind_check;
alter table reports add constraint reports_kind_check
  check (kind in ('accident','construction','jam','police','pothole','object','hazard','cheap_gas'));

-- [2] add the price column (dollars per gallon, always this unit for US gas stations)
alter table reports add column if not exists price_usd_per_gallon numeric(4,2);

-- price must be null for anything other than cheap_gas, and must be
-- a plausible value when present (greater than 0, less than 20 —
-- a minimal safeguard against joke/garbage values)
alter table reports drop constraint if exists price_only_for_cheap_gas;
alter table reports add constraint price_only_for_cheap_gas
  check (
    (kind = 'cheap_gas' and price_usd_per_gallon is not null
      and price_usd_per_gallon > 0 and price_usd_per_gallon < 20)
    or
    (kind != 'cheap_gas' and price_usd_per_gallon is null)
  );


-- =============================================================
--  Verification — try running these
-- =============================================================
-- Check the column was created correctly
--   select column_name, data_type from information_schema.columns
--   where table_name = 'reports' and column_name = 'price_usd_per_gallon';
--
-- Check a cheap_gas report goes through (swap in coordinates near a real gas station)
--   insert into reports (user_id, kind, lat, lon, price_usd_per_gallon)
--   values (auth.uid(), 'cheap_gas', 34.05, -118.24, 4.29);
--
-- Check invalid values are blocked (both of these should fail)
--   insert into reports (user_id, kind, lat, lon, price_usd_per_gallon)
--   values (auth.uid(), 'accident', 34.05, -118.24, 4.29);  -- accident with a price → error
--   insert into reports (user_id, kind, lat, lon, price_usd_per_gallon)
--   values (auth.uid(), 'cheap_gas', 34.05, -118.24, null); -- cheap_gas with no price → error
