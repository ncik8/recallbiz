"""One-shot helper to mark a user as tester. Run via Railway env (which has valid Supabase keys).

Usage:
    railway run --service recallbiz-bot -- python /tmp/mark_tester.py <telegram_username>
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "bot"))  # add bot/ for db.py
import db

if len(sys.argv) != 2:
    print("Usage: mark_tester.py <telegram_username>")
    sys.exit(1)

username = sys.argv[1].lstrip("@").lower()
user = db.find_user_by_username(username)
if not user:
    print(f"NOT FOUND: no users row with telegram_username={username!r}")
    print("(They need to /start the bot first.)")
    sys.exit(2)

print(f"Found: id={user.get('id')} email={user.get('email')} plan={user.get('plan')} is_tester={user.get('is_tester')}")
result = db.set_user_tester(user["id"], True)
print(f"set_user_tester({user['id']!r}, True) -> {result}")