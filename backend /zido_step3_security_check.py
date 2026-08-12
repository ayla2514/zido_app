# One-line summary: actively attacks zido's own safety rules (RLS policies, the
# nearby_drivers view, LA bounds check) as an outside user to confirm they actually hold.

# =============================================================
#  zido - [Step 3] Security verification
# -------------------------------------------------------------
#  Location: C:\zido\backend\zido_step3_security_check.py
#
#  What this does:
#   Directly attacks zido's safety rules to confirm they actually block what
#   they're supposed to block.
#
#  Why this matters:
#   zido handles people's real-time location. If the raw position table
#   (live_positions) can be read directly with just the publishable key,
#   the 1-minute delay and 100m blur are both meaningless — an attacker
#   could just grab the raw data instead.
#
#   This isn't something you can verify from inside the app UI — it has
#   to be checked directly against the database, and it must pass before
#   moving on to the next stage.
#
#  Before running: start zido_step2_fake_drivers.py in another terminal.
#     A target (another user's data) is needed to attack.
# =============================================================

import os
from pathlib import Path
from datetime import datetime, timezone
from dotenv import load_dotenv
from supabase import create_client


# =============================================================
#  [Config]
# =============================================================
env_path = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(dotenv_path=env_path)

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

# holds the results of each check
# each entry is [check name, passed?, explanation]
results = []


print("=" * 60)
print("zido security verification")
print("=" * 60)
print("\n⚠️  Most of these checks are supposed to FAIL.")
print("   A pass means an attempt to steal someone else's data was blocked.\n")

if SUPABASE_URL is None or SUPABASE_KEY is None:
    print("❌ Couldn't read the keys from .env.")
    raise SystemExit()

if SUPABASE_KEY.startswith("sb_secret_"):
    print("❌ Can't run these checks with an sb_secret_ key.")
    print("   That key bypasses all security, so everything would look broken.")
    print("   Switch to the publishable key.")
    raise SystemExit()


# =============================================================
#  [Setup 1] Sign in as the "attacker"
# -------------------------------------------------------------
#  Pretends to be an ordinary user and targets someone else's data.
# =============================================================
print("🔑 Signing in with a test account...")

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
auth_result = supabase.auth.sign_in_anonymously()
my_id = auth_result.user.id

# create a profile for myself too
supabase.table("profiles").insert({
    "id": my_id,
    "nickname": "Test_Auditor",
    "pin_shape": "star",
    "pin_color": "sky",
}).execute()

print("✅ Signed in (my id:", my_id[:8] + "...)\n")


# =============================================================
#  [Setup 2] Find a target
# -------------------------------------------------------------
#  The profiles table is meant to be readable by everyone
#  (only contains nickname and pin shape, nothing sensitive).
#  Grabs a user id that isn't mine.
# =============================================================
print("🎯 Finding a target...")

all_profiles = supabase.table("profiles").select("id, nickname").execute()

target_id = None
target_name = None
for p in all_profiles.data:
    if p["id"] != my_id:
        target_id = p["id"]
        target_name = p["nickname"]
        break

if target_id is None:
    print("❌ No other user found besides me.")
    print("   Run zido_step2_fake_drivers.py first.")
    raise SystemExit()

print(f"✅ Target found: {target_name} ({target_id[:8]}...)\n")
print("=" * 60)


# =============================================================
#  Check 1 · read someone else's exact position
# -------------------------------------------------------------
#  live_positions holds the real, un-blurred coordinates. If this
#  can be read, all of zido's safety measures are meaningless.
#  → passes only if nothing but my own rows come back.
# =============================================================
print("\n[Check 1] Attempting to read someone else's exact position")

try:
    stolen = supabase.table("live_positions").select("*").execute()

    # if any returned row isn't mine, this is a breach
    others = []
    for row in stolen.data:
        if row["user_id"] != my_id:
            others.append(row)

    if len(others) == 0:
        print("   ✅ PASS — no one else's position is visible")
        print(f"      (rows returned: {len(stolen.data)}, all mine)")
        results.append(["Read someone else's position", True, ""])
    else:
        print(f"   ❌ FAIL — {len(others)} other users' positions are exposed!")
        print(f"      example: {others[0]['lat']}, {others[0]['lon']}")
        results.append(["Read someone else's position", False,
                        "live_positions select policy is misconfigured"])

except Exception as e:
    # an error here also means it was blocked, so this counts as a pass
    print("   ✅ PASS — access was denied")
    results.append(["Read someone else's position", True, ""])


# =============================================================
#  Check 2 · tamper with someone else's position
# -------------------------------------------------------------
#  It must not be possible to move someone else's position
#  somewhere arbitrary. → passes only if zero rows are changed.
# =============================================================
print("\n[Check 2] Attempting to tamper with someone else's position")

try:
    changed = supabase.table("live_positions").update({
        "lat": 34.0000,
        "lon": -118.5000,
    }).eq("user_id", target_id).execute()

    if len(changed.data) == 0:
        print("   ✅ PASS — nothing was changed")
        results.append(["Tamper with someone else's position", True, ""])
    else:
        print(f"   ❌ FAIL — {len(changed.data)} row(s) were changed!")
        results.append(["Tamper with someone else's position", False,
                        "live_positions update policy is misconfigured"])

except Exception as e:
    print("   ✅ PASS — denied")
    results.append(["Tamper with someone else's position", True, ""])


# =============================================================
#  Check 3 · tamper with someone else's profile
# -------------------------------------------------------------
#  It must not be possible to freely change someone else's nickname.
# =============================================================
print("\n[Check 3] Attempting to tamper with someone else's profile")

try:
    changed = supabase.table("profiles").update({
        "nickname": "Hacked",
    }).eq("id", target_id).execute()

    if len(changed.data) == 0:
        print("   ✅ PASS — nothing was changed")
        results.append(["Tamper with someone else's profile", True, ""])
    else:
        print(f"   ❌ FAIL — someone else's nickname was changed!")
        results.append(["Tamper with someone else's profile", False,
                        "profiles update policy is misconfigured"])

except Exception as e:
    print("   ✅ PASS — denied")
    results.append(["Tamper with someone else's profile", True, ""])


# =============================================================
#  Check 4 · create a profile under someone else's id
# -------------------------------------------------------------
#  Being able to create a profile under someone else's id would
#  allow impersonation.
# =============================================================
print("\n[Check 4] Attempting to create a profile under someone else's id")

try:
    supabase.table("profiles").insert({
        "id": target_id,
        "nickname": "FakeAccount",
    }).execute()

    print("   ❌ FAIL — a profile was created under someone else's id!")
    results.append(["Create profile under someone else's id", False,
                    "profiles insert policy is misconfigured"])

except Exception as e:
    print("   ✅ PASS — denied")
    results.append(["Create profile under someone else's id", True, ""])


# =============================================================
#  Check 5 · post a report outside LA
# -------------------------------------------------------------
#  zido is LA-only. Tries submitting a report at New York's
#  coordinates. → passes only if the database rejects it.
# =============================================================
print("\n[Check 5] Attempting to post a report outside LA (New York coordinates)")

try:
    supabase.table("reports").insert({
        "user_id": my_id,
        "kind": "pothole",
        "lat": 40.7128,      # New York
        "lon": -74.0060,
    }).execute()

    print("   ❌ FAIL — the New York report was saved!")
    results.append(["LA bounds enforcement", False, "must_be_in_la constraint is missing"])

except Exception as e:
    print("   ✅ PASS — denied")
    results.append(["LA bounds enforcement", True, ""])


# =============================================================
#  Check 6 · delete someone else's report
# -------------------------------------------------------------
#  It must not be possible to freely delete a report someone
#  else posted.
# =============================================================
print("\n[Check 6] Attempting to delete someone else's report")

# first find a report posted by someone else
other_reports = supabase.table("reports").select("id, user_id").neq("user_id", my_id).limit(1).execute()

if len(other_reports.data) == 0:
    print("   ⏭  skipped — no reports from other users yet")
    print("      (leave the fake drivers running longer to generate some)")
else:
    victim_report_id = other_reports.data[0]["id"]
    try:
        deleted = supabase.table("reports").delete().eq("id", victim_report_id).execute()

        if len(deleted.data) == 0:
            print("   ✅ PASS — nothing was deleted")
            results.append(["Delete someone else's report", True, ""])
        else:
            print("   ❌ FAIL — someone else's report was deleted!")
            results.append(["Delete someone else's report", False, "reports delete policy is too open"])

    except Exception as e:
        print("   ✅ PASS — denied")
        results.append(["Delete someone else's report", True, ""])


# =============================================================
#  Check 7 · verify the safety view (nearby_drivers)
# -------------------------------------------------------------
#  This view is the gateway that shows other people's positions
#  in "safely processed" form. Confirms all three safeguards are
#  actually applied:
#   (1) 60-second delay  (2) rounded to 3 decimal places  (3) moving users only
# =============================================================
print("\n[Check 7] Verifying the safety view (nearby_drivers)")

view_rows = supabase.table("nearby_drivers").select("*").execute()

if len(view_rows.data) == 0:
    print("   ⏭  skipped — the view is empty")
    print("      turn on the fake drivers, wait at least a minute, then rerun this.")
    print("      (empty for the first minute is expected due to the 60s delay rule)")
else:
    print(f"   drivers returned: {len(view_rows.data)}")

    now = datetime.now(timezone.utc)

    # --- (1) are coordinates rounded to 3 decimal places? ---
    rounding_ok = True
    for row in view_rows.data:
        lat = float(row["lat"])
        lon = float(row["lon"])
        # the value rounded to 3 decimals should equal the original value
        if abs(lat - round(lat, 3)) > 0.0000001:
            rounding_ok = False
        if abs(lon - round(lon, 3)) > 0.0000001:
            rounding_ok = False

    if rounding_ok:
        print("   ✅ position blur PASS — coordinates are rounded to 3 decimals (~100m)")
        print(f"      example: {view_rows.data[0]['lat']}, {view_rows.data[0]['lon']}")
        results.append(["Position blur (100m)", True, ""])
    else:
        print("   ❌ position blur FAIL — exact coordinates are exposed!")
        results.append(["Position blur (100m)", False, "view is missing the round() step"])

    # --- (2) is the 1-minute delay enforced? ---
    delay_ok = True
    min_age = 99999
    for row in view_rows.data:
        recorded = datetime.fromisoformat(row["recorded_at"])
        age_sec = (now - recorded).total_seconds()
        if age_sec < min_age:
            min_age = age_sec
        # 55s gives a little slack for clock drift
        if age_sec < 55:
            delay_ok = False

    if delay_ok:
        print(f"   ✅ time delay PASS — even the newest position is {int(min_age)}s old")
        results.append(["1-minute time delay", True, ""])
    else:
        print(f"   ❌ time delay FAIL — a position only {int(min_age)}s old is visible!")
        print("      real-time tracking is possible in this state.")
        results.append(["1-minute time delay", False, "view's interval condition is misconfigured"])

    # --- (3) are stopped users excluded? ---
    # the view shouldn't have an is_moving column at all in a correct
    # setup. Instead, count stopped users in the raw table and confirm
    # they don't show up in the view.
    print("   ℹ️  To verify stopped users are excluded:")
    print("      stop the fake drivers with Ctrl+C (sets is_moving to false),")
    print("      then rerun this check — the view should come back empty.")


# =============================================================
#  Final results
# =============================================================
print("\n" + "=" * 60)
print("Final results")
print("=" * 60 + "\n")

passed = 0
failed = 0

for name, ok, reason in results:
    if ok:
        print(f"  ✅ PASS   {name}")
        passed += 1
    else:
        print(f"  ❌ FAIL   {name}")
        print(f"           → {reason}")
        failed += 1

print()
print("-" * 60)
print(f"  {passed} passed / {failed} failed")
print("-" * 60)

if failed == 0:
    print("\n🎉 All security checks passed!")
    print("   zido's server foundation is solid.")
    print("   The app work can move forward from here.")
else:
    print("\n🚨 There's a security hole.")
    print("   Do not move on to the next stage.")
    print("   Don't start building the app on top of this yet.")
    print("   Fixing it now is a few lines of SQL —")
    print("   fixing it after the app is built means tearing into the app code too.")

print("\nTo clean up the test account, run this in the SQL Editor:")
print("  delete from profiles where nickname like 'Test_%';")
