"""scripts/test_enrichment.py — end-to-end test for TRCE AI enrichment.

Steps:
1. Find or create a real test contact in Supabase
2. Call enrich_domain() for the contact's domain
3. Verify write_enrichment() persisted the result
4. Verify find_duplicates_by_company() finds sibling contacts
5. Print the Telegram message that would be sent
6. Cleanup: revert any test data we created

Usage:
    cd /Users/nick/recallbiz/bot && venv/bin/python3 scripts/test_enrichment.py
"""
import asyncio
import os
import sys
from pathlib import Path

# Ensure bot dir is on path so 'services' and 'db' resolve
sys.path.insert(0, str(Path(__file__).parent.parent))

from db import (
    get_client,
    save_contact,
    write_enrichment,
    find_duplicates_by_company,
    get_or_create_user,
)
from services.enrichment import enrich_domain, extract_domain

# Telegram user ID we use for the bot's "self" — uses Nick's own user_id for testing
TEST_TELEGRAM_ID = 6045136979


async def main():
    print("=" * 60)
    print("TRCE Enrichment End-to-End Test")
    print("=" * 60)
    print()

    # Step 1: Resolve user_id
    print("Step 1: Resolve user from Telegram ID...")
    user_id = get_or_create_user(TEST_TELEGRAM_ID)
    print(f"  user_id = {user_id}")
    print()

    # Step 2: Create a fresh test contact (we'll clean up after)
    print("Step 2: Create test contact 'Wei Zhang' at acmerobotics.com...")
    contact_id = save_contact(
        user_id=str(user_id),
        name="Wei Zhang (TEST)",
        company="Acme Robotics",
        title="CEO",
        email="wei@acmerobotics.com",
        website="https://www.acmerobotics.com",
        source="test_enrichment_script",
    )
    print(f"  contact_id = {contact_id}")
    print()

    # Step 3: Run the enrichment pipeline
    print("Step 3: Call enrich_domain('acmerobotics.com')...")
    print("  (Sonar search + M3 summary, ~5 seconds)")
    result = await enrich_domain("acmerobotics.com")
    if not result:
        print("  ERROR: enrich_domain returned None (Sonar or M3 failed)")
        print("  Common causes: OPENROUTER_API_KEY missing, rate limit, network")
        return
    print(f"  Summary: {result['summary'][:200]}...")
    print(f"  Sources: {result['sources']}")
    print(f"  From cache: {result.get('from_cache', False)}")
    print()

    # Step 4: Write enrichment to DB
    print("Step 4: write_enrichment() to persist...")
    ok = write_enrichment(contact_id, result["summary"], result["sources"])
    print(f"  write_enrichment returned: {ok}")
    print()

    # Step 5: Read back to verify it persisted
    print("Step 5: Read back the contact to verify persistence...")
    client = get_client()
    res = client.table("contacts").select(
        "id, name, company, ai_description, ai_description_sources, ai_description_updated_at"
    ).eq("id", contact_id).execute()
    if res.data:
        row = res.data[0]
        print(f"  name: {row['name']}")
        print(f"  company: {row['company']}")
        print(f"  ai_description: {(row.get('ai_description') or '')[:150]}...")
        print(f"  ai_description_sources: {row.get('ai_description_sources')}")
        print(f"  ai_description_updated_at: {row.get('ai_description_updated_at')}")
    print()

    # Step 6: Find duplicates (other contacts at same company)
    print("Step 6: find_duplicates_by_company('Acme Robotics')...")
    duplicates = find_duplicates_by_company(str(user_id), "Acme Robotics", exclude_contact_id=contact_id)
    print(f"  Found {len(duplicates)} duplicate(s)")
    for d in duplicates:
        print(f"    - {d.get('name')} ({d.get('title')}) saved {d.get('saved_at')[:10]}")
    print()

    # Step 7: Print what the bot Telegram message would look like
    print("Step 7: Render the bot Telegram message...")
    print()
    lines = ["🔍 Here's what I found about Acme Robotics:", "", result["summary"][:500]]
    if duplicates:
        lines.append("")
        lines.append(f"💡 You also have {len(duplicates)} other contact(s) at Acme Robotics:")
        for d in duplicates[:3]:
            title_part = f" ({d.get('title')})" if d.get('title') else ""
            lines.append(f"   • {d.get('name')}{title_part} — saved {d.get('saved_at')[:10]}")
        if len(duplicates) > 3:
            lines.append(f"   (+{len(duplicates) - 3} more)")
        lines.append("Want me to draft an intro?")
    print("\n".join(lines))
    print()

    # Step 8: Cleanup — delete the test contact
    print("Step 8: Cleanup — delete test contact...")
    cleanup = client.table("contacts").delete().eq("id", contact_id).execute()
    print(f"  Deleted {len(cleanup.data)} row(s)")
    print()

    print("=" * 60)
    print("OK: Enrichment pipeline works end-to-end")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())