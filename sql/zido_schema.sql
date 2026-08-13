-- One-line summary: original database schema for the LA driver community app — profiles,
-- delayed/blurred live positions, road reports, reactions, blocking, RLS policies, realtime, and cleanup.

-- =============================================================
--  LA Driver Community App - Database Schema
-- -------------------------------------------------------------
--  Usage:
--   1. Sign up at supabase.com → New Project (free)
--   2. Click SQL Editor in the left menu
--   3. Copy this whole file → paste → RUN
--
--  No Mac needed. A browser is enough.
--  If something errors, try running it in smaller chunks from the top.
-- =============================================================


-- =============================================================
--  [1] LA bounds check function
-- -------------------------------------------------------------
--  This app is LA-only.
--  Checking this in app code could be bypassed, so the database
--  itself is made to reject anything outside LA.
-- =============================================================

create or replace function is_in_la(lat double precision, lon double precision)
returns boolean as $$
  select lat between 33.70 and 34.35
     and lon between -118.70 and -118.10;
$$ language sql immutable;


-- =============================================================
--  [2] User profiles
-- -------------------------------------------------------------
--  Key rule: real names or emails are never shown to other users.
--  All another user ever sees is the nickname and pin shape.
-- =============================================================

create table profiles (
  id            uuid primary key references auth.users(id) on delete cascade,
  nickname      text not null,

  -- pin customization (no free uploads — chosen from preset options only)
  pin_shape     text not null default 'car'
                check (pin_shape in ('car','cat','dog','heart','star','rocket','bear','coffee')),
  pin_color     text not null default 'mint'
                check (pin_color in ('mint','peach','lavender','sky','lemon','rose')),

  created_at    timestamptz not null default now()
);

-- limit nickname length (too long clutters the map)
alter table profiles add constraint nickname_length
  check (char_length(nickname) between 1 and 12);


-- =============================================================
--  [3] Live position — the heart of this app
-- -------------------------------------------------------------
--  The app overwrites its own position here every 15 seconds.
--  Important: this table is never shown to other users directly.
--     It's only ever exposed through the "delayed and blurred
--     view" in [4] below.
-- =============================================================

create table live_positions (
  user_id       uuid primary key references profiles(id) on delete cascade,
  lat           double precision not null,
  lon           double precision not null,
  heading       double precision,              -- direction the car is facing (0-360 deg)
  speed_mps     double precision,              -- speed (meters per second)
  is_moving     boolean not null default false,-- currently moving?
  recorded_at   timestamptz not null default now(),

  -- reject coordinates outside LA entirely
  constraint must_be_in_la check (is_in_la(lat, lon))
);

-- index to quickly find old positions
create index idx_positions_time on live_positions (recorded_at);


-- =============================================================
--  [4] Safety-processed position view — the most important part
-- -------------------------------------------------------------
--  Three layers of protection applied before showing data to
--  other users.
--
--   (1) Time delay : only show positions older than 60 seconds
--                    -> makes real-time tracking impossible
--   (2) Position blur : round to 3 decimal places (~100m grid)
--                    -> only reveals "roughly here," not an exact spot
--   (3) Moving only : someone standing still is never shown at all
--                    -> prevents revealing home/work locations
--
--  Removing any one of these three turns this into a stalking
--  tool. Never remove any of them.
-- =============================================================

create or replace view nearby_drivers as
select
  p.id                          as user_id,
  p.nickname,
  p.pin_shape,
  p.pin_color,
  round(lp.lat::numeric, 3)     as lat,      -- (2) blur to ~100m
  round(lp.lon::numeric, 3)     as lon,
  lp.heading,
  lp.recorded_at
from live_positions lp
join profiles p on p.id = lp.user_id
where lp.is_moving = true                              -- (3) moving only
  and lp.recorded_at < now() - interval '60 seconds'   -- (1) at least 1 min old
  and lp.recorded_at > now() - interval '5 minutes';   --     exclude very stale records


-- =============================================================
--  [5] Road condition reports
-- =============================================================

create table reports (
  id            bigserial primary key,
  user_id       uuid not null references profiles(id) on delete cascade,

  kind          text not null
                check (kind in ('accident','construction','jam','police','pothole','object','hazard')),

  lat           double precision not null,
  lon           double precision not null,

  created_at    timestamptz not null default now(),
  expires_at    timestamptz not null default now() + interval '2 hours',

  -- other users' confirmation votes
  still_count   integer not null default 0,   -- "still there"
  gone_count    integer not null default 0,   -- "it's gone"

  is_hidden     boolean not null default false, -- hidden once enough downvotes pile up

  constraint must_be_in_la check (is_in_la(lat, lon))
);

create index idx_reports_active on reports (expires_at) where is_hidden = false;


-- prevents the same person from voting on the same report more than once
create table report_votes (
  report_id     bigint not null references reports(id) on delete cascade,
  user_id       uuid   not null references profiles(id) on delete cascade,
  vote          text   not null check (vote in ('still','gone')),
  created_at    timestamptz not null default now(),
  primary key (report_id, user_id)
);


-- =============================================================
--  [6] Reactions (sensing each other's presence)
-- -------------------------------------------------------------
--  No 1:1 messaging is built. Location-based private conversation
--  can become a channel for harassment.
--  Instead, only short reactions broadcast to everyone nearby are allowed.
-- =============================================================

create table waves (
  id            bigserial primary key,
  user_id       uuid not null references profiles(id) on delete cascade,
  emoji         text not null check (emoji in ('wave','thanks','careful','love','sorry')),
  lat           double precision not null,
  lon           double precision not null,
  created_at    timestamptz not null default now(),

  constraint must_be_in_la check (is_in_la(lat, lon))
);


-- =============================================================
--  [7] Blocking & abuse reports (required for App Store review)
-- -------------------------------------------------------------
--  Built ahead of time even though unused right now — bolting
--  this on later in a hurry tends to make the structure messy.
-- =============================================================

create table blocks (
  blocker_id    uuid not null references profiles(id) on delete cascade,
  blocked_id    uuid not null references profiles(id) on delete cascade,
  created_at    timestamptz not null default now(),
  primary key (blocker_id, blocked_id)
);

create table abuse_reports (
  id            bigserial primary key,
  reporter_id   uuid not null references profiles(id) on delete cascade,
  target_user   uuid references profiles(id) on delete cascade,
  target_report bigint references reports(id) on delete cascade,
  reason        text not null,
  created_at    timestamptz not null default now(),
  resolved_at   timestamptz
);


-- =============================================================
--  [8] Access control (RLS) — without this, anyone can tamper with anyone's data
-- -------------------------------------------------------------
--  Supabase has the app talk to the database directly, so without
--  locking permissions down here, anyone could tamper with
--  anyone else's position data.
-- =============================================================

alter table profiles       enable row level security;
alter table live_positions enable row level security;
alter table reports        enable row level security;
alter table report_votes   enable row level security;
alter table waves          enable row level security;
alter table blocks         enable row level security;
alter table abuse_reports  enable row level security;

-- profiles: visible to everyone, editable only by their owner
create policy "프로필 읽기"   on profiles for select using (true);
create policy "내 프로필 생성" on profiles for insert with check (auth.uid() = id);
create policy "내 프로필 수정" on profiles for update using (auth.uid() = id);

-- positions: only my own position can be read or written.
--            other people's positions are only ever seen through the nearby_drivers view.
create policy "내 위치만 읽기" on live_positions for select using (auth.uid() = user_id);
create policy "내 위치 올리기" on live_positions for insert with check (auth.uid() = user_id);
create policy "내 위치 갱신"   on live_positions for update using (auth.uid() = user_id);

-- reports: only non-hidden ones can be read, only your own can be written
create policy "신고 읽기"   on reports for select using (is_hidden = false);
create policy "신고 올리기" on reports for insert with check (auth.uid() = user_id);

create policy "투표 읽기"   on report_votes for select using (true);
create policy "투표 하기"   on report_votes for insert with check (auth.uid() = user_id);

create policy "반응 읽기"   on waves for select using (true);
create policy "반응 보내기" on waves for insert with check (auth.uid() = user_id);

create policy "내 차단목록"   on blocks for select using (auth.uid() = blocker_id);
create policy "차단하기"     on blocks for insert with check (auth.uid() = blocker_id);
create policy "부적절 신고"   on abuse_reports for insert with check (auth.uid() = reporter_id);


-- =============================================================
--  [9] Turn on realtime broadcasting
-- -------------------------------------------------------------
--  This needs to be on for "the moment I drop a pin, it instantly
--  shows up on other people's phones" to work.
--  (Can also be turned on from Supabase dashboard's Database > Replication)
-- =============================================================

alter publication supabase_realtime add table reports;
alter publication supabase_realtime add table waves;


-- =============================================================
--  [10] Cleaning up old data
-- -------------------------------------------------------------
--  Letting position records pile up indefinitely effectively
--  becomes a stored travel history. Old records are deleted for
--  privacy.
--  Set this to run every 10 minutes from Supabase dashboard >
--  Database > Cron.
-- =============================================================

create or replace function cleanup_old_data()
returns void as $$
begin
  delete from live_positions where recorded_at < now() - interval '10 minutes';
  delete from waves         where created_at  < now() - interval '1 hour';
  delete from reports       where expires_at  < now() - interval '1 day';
end;
$$ language plpgsql;


-- =============================================================
--  Verification: run this to confirm every table was created
-- =============================================================
-- select table_name from information_schema.tables where table_schema = 'public';
