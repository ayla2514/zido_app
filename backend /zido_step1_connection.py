# One-line summary: quick smoke test for the Python <-> Supabase connection —
# signs in anonymously, creates a test profile, then reads it back to confirm everything works.

# =============================================================
#  [Step 1] Verify Supabase connection
# -------------------------------------------------------------
#  What this does:
#   1) sign in anonymously
#   2) create a profile for myself
#   3) read it back to confirm it saved correctly
#
#  If this succeeds, the Python <-> Supabase connection is working.
#  Creating fake drivers comes after this.
#
#  One-time setup in the terminal:
#     pip install supabase
# =============================================================

import os                            # for reading .env values
from pathlib import Path             # for working with file paths
from dotenv import load_dotenv       # for loading .env files
from supabase import create_client   # for connecting to Supabase


# =============================================================
#  [Config] ← only change values here
# =============================================================

# .env lives one folder above this file (backend), at C:\zido
env_path = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(dotenv_path=env_path)

# Make sure .env has the "anon public" key.
# The "service_role" (sb_secret_) key bypasses all security rules and
# is a master key — never use it even for testing (you won't be able
# to tell what's actually being blocked).
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

if SUPABASE_URL is None or SUPABASE_KEY is None:
    print("❌ Couldn't read the keys from .env. Looked in:", env_path)
    raise SystemExit()

# info for the test profile to create
TEST_NICKNAME = "Test_MintCar"
TEST_PIN_SHAPE = "car"     # one of: car, cat, dog, heart, star, rocket, bear, coffee
TEST_PIN_COLOR = "mint"    # one of: mint, peach, lavender, sky, lemon, rose


# =============================================================
#  [1] Connect to Supabase
# =============================================================
print("=" * 55)
print("Supabase connection test")
print("=" * 55)

# opens a connection. not signed in yet at this point.
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

print("✅ Connection created\n")


# =============================================================
#  [2] Sign in anonymously
# -------------------------------------------------------------
#  Our app works with no signup required.
#  Anonymous sign-in makes Supabase create a temporary user and
#  return that user's unique id (uuid).
# =============================================================
print("🔑 Signing in anonymously...")

auth_result = supabase.auth.sign_in_anonymously()

# grab the signed-in user's unique id.
# this id is what represents "me" from here on.
my_user_id = auth_result.user.id

print("✅ Signed in")
print("   My user id:", my_user_id, "\n")


# =============================================================
#  [3] Create a profile
# -------------------------------------------------------------
#  Note: the id field must be set to the my_user_id we just signed
#  in as. There's a database rule (RLS) that says "you can only
#  create your own profile" — using a different id gets rejected.
# =============================================================
print("👤 Creating profile...")

new_profile = {
    "id": my_user_id,
    "nickname": TEST_NICKNAME,
    "pin_shape": TEST_PIN_SHAPE,
    "pin_color": TEST_PIN_COLOR,
}

# .insert() means "add a new row", .execute() means "actually run it"
insert_result = supabase.table("profiles").insert(new_profile).execute()

print("✅ Profile created\n")


# =============================================================
#  [4] Read it back to confirm
# -------------------------------------------------------------
#  .select("*")  = fetch every column   (like viewing a full pandas df)
#  .eq("id", ..) = only rows where id matches   (like df[df.id == ..])
# =============================================================
print("📖 Reading the profile back...")

read_result = supabase.table("profiles").select("*").eq("id", my_user_id).execute()

# the result comes back as a list — grab the first item.
my_profile = read_result.data[0]

print("✅ Read succeeded\n")
print("-" * 55)
print("  Nickname:", my_profile["nickname"])
print("  Pin shape:", my_profile["pin_shape"])
print("  Pin color:", my_profile["pin_color"])
print("  Created at:", my_profile["created_at"])
print("-" * 55)


# =============================================================
#  [5] Wrap up
# =============================================================
print("\n🎉 Python <-> Supabase connection succeeded!")
print("\nNext steps:")
print("  1. In the Supabase dashboard > Table Editor > profiles,")
print("     visually confirm the row you just created is there.")
print("  2. Once confirmed, move on to zido_step2_fake_drivers.py.")

print("\nNote: running this script multiple times keeps piling up test profiles.")
print("  To clean them up later, run this in the SQL Editor:")
print("  delete from profiles where nickname like 'Test_%';")
