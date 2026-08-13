-- One-line summary: adds a table + storage bucket so the app can upload sign photos directly (with location/heading/speed), replacing a manual send-and-drop-in-a-folder workflow, so photos can later be batch-downloaded, processed, and deleted.

-- =============================================================
--  zido fix 07 — in-app sign photo upload (for street parking data collection)
-- -------------------------------------------------------------
--  Why:
--   Until now, this was a manual process — taking a photo of a sign
--   on a phone, sending it over, and manually dropping it into a
--   folder on the computer. This lets the app upload a photo
--   directly the moment it's taken, so it can later be pulled and
--   processed in one batch (works fine with multiple people taking
--   photos too — this is designed to scale as a crowdsourced flow).
--
--  Structure:
--   1) the photo file itself is stored in Supabase Storage's
--      sign-photos bucket
--   2) location (lat/lon), heading (optional), speed (speed_mps,
--      optional), and capture time are recorded in this table
--      instead of relying on EXIF (photo recompression during
--      upload can strip GPS data from EXIF, so having the app
--      record values directly via CLLocationManager is much safer)
--      speed_mps is just stored for now — what to actually do with
--      it (e.g. "trust photos taken while stopped more") isn't
--      built yet per current scope, but the data is captured ahead
--      of time so it's ready whenever that's needed
--   3) photos get batch-downloaded via zido_step15, and once
--      processed, deleted immediately from both storage and this
--      table (a sign photo contains sensitive location data from
--      where it was taken, so there's no reason to keep a processed
--      original sitting on the server — decided this way per current scope)
--
--  Why read (select) / delete are left open to everyone:
--   the batch-download script also only uses the same anonymous key
--   (anon/publishable) that a signed-in-anonymously app user uses
--   (the sb_secret_ key is never put in any code, as a rule). So a
--   "only your own rows" policy would mean the batch script couldn't
--   read anyone else's uploaded photos. The reports/waves tables are
--   already open for reads for the same reason (community data is
--   meant to be public), so this follows the same pattern. Delete is
--   open for the same reason — this is a simplification that assumes
--   "there's one trusted batch-processing script," not something
--   sensitive or exploitable enough to worry about people deleting
--   each other's rows (it's just sign location data).
--
--  Usage:
--   Supabase > SQL Editor > New query > paste all > RUN
--   (apply order: schema -> fix01~06 -> fix07)
--   Afterward, check the Storage tab to visually confirm the
--   sign-photos bucket was created (step [2] below creates it
--   automatically, but dashboards can behave slightly differently,
--   so a visual check is recommended).
-- =============================================================

-- [1] create the table
create table if not exists sign_photo_uploads (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id),
  storage_path text not null,
  lat double precision not null,
  lon double precision not null,
  heading double precision,               -- compass heading at capture time (0-360, optional) — helps determine which lane a sign applies to
  speed_mps double precision,             -- vehicle speed at capture time (m/s, optional) — just stored for now, for future use as needed
  captured_at timestamptz not null default now(),
  created_at timestamptz not null default now(),
  constraint must_be_in_la check (is_in_la(lat, lon)),
  constraint heading_range check (heading is null or (heading >= 0 and heading < 360)),
  constraint speed_range check (speed_mps is null or (speed_mps >= 0 and speed_mps < 60))  -- 60m/s ≈ 134mph, a generous ceiling just to block garbage values
);

alter table sign_photo_uploads enable row level security;

drop policy if exists "표지판사진 기록 올리기" on sign_photo_uploads;
create policy "표지판사진 기록 올리기" on sign_photo_uploads
  for insert with check (auth.uid() = user_id);

drop policy if exists "표지판사진 기록 읽기" on sign_photo_uploads;
create policy "표지판사진 기록 읽기" on sign_photo_uploads
  for select using (true);

drop policy if exists "표지판사진 기록 삭제" on sign_photo_uploads;
create policy "표지판사진 기록 삭제" on sign_photo_uploads
  for delete using (true);


-- [2] create the storage bucket (private — not listable/viewable by just anyone via URL, requires an authenticated request)
insert into storage.buckets (id, name, public)
values ('sign-photos', 'sign-photos', false)
on conflict (id) do nothing;

-- upload: any signed-in user, but only into their own user_id folder
-- (path convention: "{user_id}/{filename}.jpg" — see the app-side guide)
drop policy if exists "표지판사진 업로드" on storage.objects;
create policy "표지판사진 업로드" on storage.objects
  for insert with check (
    bucket_id = 'sign-photos'
    and auth.uid()::text = (storage.foldername(name))[1]
  );

-- download/delete: open to everyone, for the same reason as the table above
-- (the batch-download script needs to read and delete any user's files using the anon key)
drop policy if exists "표지판사진 다운로드" on storage.objects;
create policy "표지판사진 다운로드" on storage.objects
  for select using (bucket_id = 'sign-photos');

drop policy if exists "표지판사진 삭제" on storage.objects;
create policy "표지판사진 삭제" on storage.objects
  for delete using (bucket_id = 'sign-photos');


-- =============================================================
--  Verification — try running these
-- =============================================================
-- Check the table was created correctly
--   select * from sign_photo_uploads limit 1;
--
-- Check the bucket was created correctly
--   select * from storage.buckets where id = 'sign-photos';
--
-- Check a coordinate outside LA is blocked (should fail)
--   insert into sign_photo_uploads (user_id, storage_path, lat, lon)
--   values (auth.uid(), 'test/x.jpg', 40.0, -74.0);
