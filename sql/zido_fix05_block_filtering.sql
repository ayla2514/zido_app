-- One-line summary: enforces mutual (two-way) blocking at the database level — once two users block each other, neither can see the other's reports, waves, or live position, regardless of what the app does.

-- =============================================================
--  zido fix 05 — hide blocked users' content starting at the DB
-- -------------------------------------------------------------
--  Why:
--   Until now, filtering out blocked users was left entirely to
--   the app (Swift). But if the app ever misses that logic or has
--   a bug, a blocked user's content still shows up. Blocking is a
--   safety feature, so it needs to be enforced at the database
--   level, not just trusted to app code.
--
--  Why this is made two-way:
--   The original blocks table design was one-directional — only
--   the person who blocked someone stops seeing them. But since
--   zido's core value is preventing stalking, one-directional
--   blocking leaves a gap: blocking someone who's harassing you
--   doesn't stop them from still seeing you. So this fix makes
--   blocking **mutual (two-way)**.
--
--  Fix:
--   Adds a two-way exclusion condition — "anyone I'm in a block
--   relationship with, either direction" — to the reports/waves
--   read policies and to the nearby_drivers view.
--
--  Usage:
--   Supabase > SQL Editor > New query > paste all > RUN
--   (apply order: schema -> fix01 -> fix02 -> fix03 -> fix04 -> fix05)
-- =============================================================


-- =============================================================
--  [1] Update the reports read policy
-- =============================================================

drop policy if exists "신고 읽기" on reports;

create policy "신고 읽기" on reports for select
  using (
    is_hidden = false
    and user_id not in (
      select blocked_id from blocks where blocker_id = auth.uid()
      union
      select blocker_id from blocks where blocked_id = auth.uid()
    )
  );


-- =============================================================
--  [2] Update the waves read policy
-- =============================================================

drop policy if exists "반응 읽기" on waves;

create policy "반응 읽기" on waves for select
  using (
    user_id not in (
      select blocked_id from blocks where blocker_id = auth.uid()
      union
      select blocker_id from blocks where blocked_id = auth.uid()
    )
  );


-- =============================================================
--  [3] Update the safety position view (add block-relationship exclusion)
-- -------------------------------------------------------------
--  The existing 3-layer safeguard (1-min delay, ~100m blur, moving
--  only) stays exactly as-is — only the block condition is added.
-- =============================================================

create or replace view nearby_drivers
with (security_invoker = off) as
select distinct on (lp.user_id)
  p.id                        as user_id,
  p.nickname,
  p.pin_shape,
  p.pin_color,
  round(lp.lat::numeric, 3)   as lat,      -- blur to ~100m
  round(lp.lon::numeric, 3)   as lon,
  lp.heading,
  lp.recorded_at
from live_positions lp
join profiles p on p.id = lp.user_id
where lp.is_moving = true                              -- moving records only
  and lp.recorded_at < now() - interval '60 seconds'   -- at least 1 min old
  and lp.recorded_at > now() - interval '5 minutes'    -- exclude very stale records
  and p.id not in (                                     -- exclude block relationships (two-way)
    select blocked_id from blocks where blocker_id = auth.uid()
    union
    select blocker_id from blocks where blocked_id = auth.uid()
  )
order by lp.user_id, lp.recorded_at desc;


-- =============================================================
--  Verification — try running these
-- =============================================================
-- Sign in as two test accounts A and B, block each other, then
-- switch between them and confirm neither shows up for the other
-- in nearby_drivers/reports/waves.
--
--   insert into blocks (blocker_id, blocked_id) values (auth.uid(), 'other_user_id');
