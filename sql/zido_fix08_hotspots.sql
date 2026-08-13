-- One-line summary: adds a hotspots table (curated cafes/food/trendy spots shown as map emoji icons), as a data structure ready to be filled once selection criteria are decided.

-- =============================================================
--  zido fix 08 — hotspots (map emoji icons)
-- -------------------------------------------------------------
--  Why:
--   Requested to show places like matcha cafes, good food spots,
--   hotspots, and trendy spots as small emoji icons on the map.
--   The selection criteria will be decided and added later, so for
--   now this just sets up the data structure needed to plot such
--   places on the map whenever they exist (same pattern as the sign
--   photo feature — build the container first, decide what goes in
--   it and how it's used later).
--
--  Structure:
--   category starts with the four currently named
--   (matcha/food/hotplace/trendy). If more are needed later, they
--   can be added the same way reports.kind was extended in fix06 —
--   via a new fix file.
--   emoji isn't a fixed value per category — it's set freely per
--   place (so even within "food," something like 🍜/🥐/🍰 can be
--   picked to match the specific spot).
--
--  Who adds entries (for now):
--   Unlike community reports (reports), this is "curated" data —
--   researched and picked by hand (or by Claude), not submitted by
--   users. So this defaults to being populated directly via script,
--   not a user-facing report flow.
--
--  Where candidates currently live:
--   Since there's no formal selection criteria yet, current
--   candidates are just collected as files under
--   data/LATER/cafes_hotspots/ (kept completely separate from the
--   live service). Once criteria are decided, they can be moved
--   into this table — this fix08 is just preparing "the container
--   to use at that point."
--
--  Why insert/update/delete are left open to everyone (same
--  reasoning as reports / sign_photo_uploads):
--   This project has no separate "real admin" login (everything is
--   anonymous sign-in), and the curation script also only uses the
--   same anon key as regular app users. At this stage, the priority
--   is getting the feature working at all, rather than the risk of
--   someone adding a joke entry — kept simple and open for now.
--   Once this is actually used by many people, it can be locked
--   down further then.
--
--  Usage:
--   Supabase > SQL Editor > New query > paste all > RUN
--   (apply order: schema -> fix01~07 -> fix08)
-- =============================================================

create table if not exists hotspots (
  id uuid primary key default gen_random_uuid(),
  name text not null,                     -- place name (e.g. "XYZ Cafe")
  lat double precision not null,
  lon double precision not null,
  category text not null,                 -- matcha / food / hotplace / trendy
  emoji text not null,                    -- one emoji to plot on the map (e.g. 🍵 🍽️ 🔥 ✨)
  note text,                              -- short note on why this was picked (free text — this is curated data added directly by a human/Claude, not a user report, so it's not subject to reports' "no free text" rule)
  source_url text,                        -- reference link if any — makes it easier to verify/update later
  added_by uuid references auth.users(id),
  created_at timestamptz not null default now(),
  constraint must_be_in_la check (is_in_la(lat, lon)),
  constraint hotspot_category_check check (category in ('matcha','food','hotplace','trendy'))
);

alter table hotspots enable row level security;

drop policy if exists "핫플 읽기" on hotspots;
create policy "핫플 읽기" on hotspots for select using (true);

drop policy if exists "핫플 추가" on hotspots;
create policy "핫플 추가" on hotspots for insert with check (true);

drop policy if exists "핫플 수정" on hotspots;
create policy "핫플 수정" on hotspots for update using (true);

drop policy if exists "핫플 삭제" on hotspots;
create policy "핫플 삭제" on hotspots for delete using (true);


-- =============================================================
--  Verification — try running these
-- =============================================================
-- Check the table was created correctly
--   select * from hotspots limit 1;
--
-- Check a coordinate outside LA is blocked (should fail)
--   insert into hotspots (name, lat, lon, category, emoji)
--   values ('test', 40.0, -74.0, 'food', '🍽️');
--
-- Check an invalid category is blocked (should fail)
--   insert into hotspots (name, lat, lon, category, emoji)
--   values ('test', 34.05, -118.24, 'nightclub', '🎉');
