-- One-line summary: schedules the existing cleanup_old_data() function to run every 10 minutes via pg_cron, directly in SQL instead of a manually-clicked dashboard setting.

-- =============================================================
--  zido fix 04 — lock in the old-data cleanup schedule as SQL
-- -------------------------------------------------------------
--  Why:
--   cleanup_old_data() already existed in schema.sql, but running
--   it "every 10 minutes" had to be set up by hand in the Supabase
--   dashboard. Dashboard settings don't live in a file, so it was
--   never clear whether the schedule was actually still in place.
--
--   This matters for safety directly — if position records aren't
--   cleared within 10 minutes, that data effectively becomes a
--   stored travel history, and a stopped location (home/work) stays
--   around longer than it should.
--
--  Fix:
--   Uses the pg_cron extension to set the schedule directly in SQL.
--   Now this one file both confirms whether the schedule exists and
--   (re)creates it.
--
--  Usage:
--   Supabase > SQL Editor > New query > paste all > RUN
--
--   If you get an "extension pg_cron does not exist" error:
--   go to Supabase dashboard > Database > Extensions, search for
--   pg_cron, enable it, then run this file again.
--
--   Safe to run multiple times (it clears any existing job with the
--   same name before recreating it).
-- =============================================================

create extension if not exists pg_cron;

-- clear any previous schedule with the same name (avoids duplicates)
do $$
begin
  if exists (select 1 from cron.job where jobname = 'zido-cleanup') then
    perform cron.unschedule('zido-cleanup');
  end if;
end $$;

-- run cleanup_old_data() every 10 minutes
select cron.schedule(
  'zido-cleanup',
  '*/10 * * * *',
  $$select cleanup_old_data()$$
);


-- =============================================================
--  Verification — try running these
-- =============================================================
-- Check the schedule is in place
--   select jobname, schedule, active from cron.job where jobname = 'zido-cleanup';
--
-- Check it has actually run (wait a few minutes, then check)
--   select * from cron.job_run_details
--   where jobid = (select jobid from cron.job where jobname = 'zido-cleanup')
--   order by start_time desc limit 5;
