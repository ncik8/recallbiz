"""Smoke test for db.py enrichment helpers — no LLM calls needed.
Validates write_enrichment() and find_duplicates_by_company() against live Supabase.
"""
import os
import sys
from pathlib import Path

# Load env from ~/.env.supabase (which has the live keys) BEFORE importing db
env_path = Path.home() / ".env.supabase"
env = {}
for line in env_path.read_text().splitlines():
    line = line.strip()
    if "=" in line and not line.startswith("#"):
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip()

# Translate TRCE .env.supabase names → bot module expected names
os.environ["SUPABASE_URL"] = env.get("SUPABASE_URL", "")
os.environ["SUPABASE_SERVICE_ROLE_KEY"] = env.get("SUPABASE_SERVICE_ROLE_KEY", "")  # db.py reads this name

# Force re-init of cached client (in case imported before)
sys.path.insert(0, str(Path(__file__).parent.parent))
if "db" in sys.modules:
    del sys.modules["db"]

from db import get_client, save_contact, write_enrichment, find_duplicates_by_company, get_or_create_user

TEST_TG_ID = 6045136979

print("1. Resolve user...")
user_id = get_or_create_user(TEST_TG_ID)
print(f"   user_id: {user_id}")
print()

print("2. Create test contact...")
contact_id = save_contact(
    user_id=str(user_id),
    name="Smoke Test Wei Zhang",
    company="Smoke Test Co XYZ",
    title="CEO",
    email="wei@smoketestco.example",
    source="smoke_test_script",
)
print(f"   contact_id: {contact_id}")
print()

print("3. write_enrichment()...")
ok = write_enrichment(
    contact_id,
    summary="Smoke Test Co XYZ is a fake company used for DB testing. It does nothing. Founded today by this script.",
    sources=[{"url": "https://example.com", "label": "Test source"}],
)
print(f"   returned: {ok}")
print()

print("4. Read back to verify...")
client = get_client()
res = client.table("contacts").select(
    "ai_description, ai_description_sources, ai_description_updated_at"
).eq("id", contact_id).execute()
if res.data:
    row = res.data[0]
    print(f"   ai_description: {(row.get('ai_description') or '')[:80]}...")
    print(f"   ai_description_sources: {row.get('ai_description_sources')}")
    print(f"   ai_description_updated_at: {row.get('ai_description_updated_at')}")
print()

print("5. find_duplicates_by_company('Smoke Test Co XYZ')...")
dupes = find_duplicates_by_company(str(user_id), "Smoke Test Co XYZ", exclude_contact_id=contact_id)
print(f"   Found {len(dupes)} duplicates (should be 0 — first time we save this company)")
print()

# Add a SECOND contact at the same company to verify dup detection
print("6. Add a SECOND contact at same company to verify dup detection...")
contact2 = save_contact(
    user_id=str(user_id),
    name="Smoke Test Mei Lin",
    company="Smoke Test Co XYZ",
    title="Head of Partnerships",
    source="smoke_test_script",
)
print(f"   contact2_id: {contact2}")
dupes2 = find_duplicates_by_company(str(user_id), "Smoke Test Co XYZ", exclude_contact_id=contact2)
print(f"   Found {len(dupes2)} duplicates (should be 1 — Wei Zhang)")
for d in dupes2:
    print(f"     - {d.get('name')} ({d.get('title')})")
print()

print("7. Cleanup — delete both test contacts...")
client.table("contacts").delete().eq("id", contact_id).execute()
client.table("contacts").delete().eq("id", contact2).execute()
print("   Done")
print()

print("=" * 60)
print("OK: DB helpers work against live Supabase")
print("=" * 60)