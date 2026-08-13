-- One-line summary: auto-tallies report upvotes/downvotes via a trigger and auto-hides a report once "gone" votes outnumber "still" votes by 3+.

-- =============================================================
--  zido fix 03 — auto-tally report votes + auto-hide
-- -------------------------------------------------------------
--  Why:
--   Votes pile up in report_votes, but reports.still_count/gone_count
--   never increase automatically. Tapping the vote button in the app
--   doesn't change the number shown on screen.
--
--   Also, reports.is_hidden was originally meant for "auto-hide once
--   enough downvotes pile up," but there was no code that actually
--   did that.
--
--  Fix:
--   Whenever a new vote lands in report_votes (via trigger), the
--   matching row in reports gets its count updated automatically.
--   Once "gone" outnumbers "still" by 3 or more, the report is
--   auto-hidden (the threshold can be tuned later based on real use).
--
--  What security definer means here:
--   Regular users normally can't update the reports table directly
--   (deliberately locked down so no one can tamper with the counts).
--   But this trigger only performs one controlled action — bump one
--   count per incoming vote — so the trigger function alone is
--   granted an exception. This does NOT give users direct write
--   access to reports.
--
--  Usage:
--   Supabase > SQL Editor > New query > paste all > RUN
--   (apply order: schema -> fix01 -> fix02 -> fix03)
-- =============================================================

create or replace function apply_report_vote()
returns trigger as $$
begin
  if new.vote = 'still' then
    update reports set still_count = still_count + 1 where id = new.report_id;
  elsif new.vote = 'gone' then
    update reports set gone_count = gone_count + 1 where id = new.report_id;
  end if;
  -- auto-hide once "gone" outnumbers "still" by 3 or more
  -- (3 is an arbitrary starting value — adjust if reports end up
  --  getting hidden too easily or not easily enough in practice)
  update reports
  set is_hidden = true
  where id = new.report_id
    and gone_count - still_count >= 3
    and is_hidden = false;
  return new;
end;
$$ language plpgsql security definer;

drop trigger if exists trg_apply_report_vote on report_votes;
create trigger trg_apply_report_vote
  after insert on report_votes
  for each row execute function apply_report_vote();


-- =============================================================
--  Verification — try running these
-- =============================================================
-- Check the trigger got attached correctly
--   select tgname from pg_trigger where tgname = 'trg_apply_report_vote';
--
-- Check that voting actually bumps the counts (swap in any real report id)
--   insert into report_votes (report_id, user_id, vote)
--   values (1, auth.uid(), 'still');
--   select id, still_count, gone_count, is_hidden from reports where id = 1;
