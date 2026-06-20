"""Supabase-backed DB layer for TRCE (formerly RecallBiz).

Multi-tenant: every function takes `user_id` (the INTERNAL users.id UUID,
not the telegram_user_id). Call `get_or_create_user(telegram_user_id)`
once per command to resolve it.

Auth boundary: telegram_user_id IS the identity in v0.1. v0.2 will link
this to a Supabase auth user (so users can also log in via web).
"""
import os
import logging
from typing import Optional
from datetime import datetime, timezone, timedelta
from supabase import create_client, Client
import bcrypt

log = logging.getLogger(__name__)

# bcrypt cost factor. 12 is the modern default — ~250ms on commodity hardware.
# Don't go lower; don't go higher without load-testing.
BCRYPT_ROUNDS = 12

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")

_client: Optional[Client] = None


def get_client() -> Client:
    """Lazy-init Supabase client. Single instance per process."""
    global _client
    if _client is None:
        if not SUPABASE_URL or not SUPABASE_KEY:
            raise RuntimeError(
                "SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be set"
            )
        _client = create_client(SUPABASE_URL, SUPABASE_KEY)
        log.info("Supabase client initialized (%s)", SUPABASE_URL[:30])
    return _client


def init_db():
    """Verify Supabase connection. Tables managed in SQL Editor — no DDL here."""
    client = get_client()
    res = client.table("users").select("id").limit(1).execute()
    log.info("Supabase connection OK (users table reachable)")


def get_or_create_user(telegram_user_id: int, username: str = None,
                       display_name: str = None) -> str:
    """Resolve telegram_user_id -> internal users.id UUID. Creates row if new."""
    client = get_client()
    # Use limit(1).execute() — NOT maybe_single() — because maybe_single
    # returns None when no row matches, causing AttributeError on res.data.
    res = (
        client.table("users")
        .select("id")
        .eq("telegram_user_id", telegram_user_id)
        .limit(1)
        .execute()
    )
    if res.data and len(res.data) > 0:
        client.table("users").update({"last_active_at": "now()"}).eq(
            "id", res.data[0]["id"]
        ).execute()
        return res.data[0]["id"]  
    res = (
        client.table("users")
        .insert({
            "telegram_user_id": telegram_user_id,
            "telegram_username": username,
            "display_name": display_name,
        })
        .execute()
    )
    user_id = res.data[0]["id"]
    log.info("Created new user %s for telegram_user_id=%s", user_id, telegram_user_id)
    _seed_starter_tags(user_id)
    return user_id


def _seed_starter_tags(user_id: str):
    """Per-user starter tags. Idempotent (UNIQUE constraint catches dupes)."""
    client = get_client()
    starter = ["investor", "founder", "speaker", "team", "press"]
    try:
        client.table("tags").insert([
            {"user_id": user_id, "name": t} for t in starter
        ]).execute()
    except Exception:
        pass


def log_usage(user_id: str, action: str, details: str = None):
    """Light audit log. Failures here must NEVER break a real command."""
    try:
        get_client().table("usage").insert({
            "user_id": user_id,
            "action": action,
            "details": details,
        }).execute()
    except Exception as e:
        log.warning("log_usage failed: %s", e)


def save_contact(
    user_id: str,
    name: str,
    handle: str = None,
    company: str = None,
    title: str = None,
    email: str = None,
    phone: str = None,
    website: str = None,
    notes: str = None,
    source: str = "manual",
    telegram_user_id: int = None,
) -> str:
    """Insert a contact. Returns the new contact's UUID. Auto-tags with active trip."""
    client = get_client()
    row = {
        "user_id": user_id,
        "name": name,
        "handle": handle,
        "company": company,
        "title": title,
        "email": email,
        "phone": phone,
        "website": website,
        "notes": notes,
        "source": source,
        "telegram_user_id": telegram_user_id,
    }
    row = {k: v for k, v in row.items() if v is not None}
    res = client.table("contacts").insert(row).execute()
    contact_id = res.data[0]["id"]

    trip = get_active_trip(user_id)
    if trip:
        try:
            client.table("contact_events").insert({
                "contact_id": contact_id,
                "event_id": trip["id"],
                "context": f"auto-tagged via {source}",
            }).execute()
        except Exception as e:
            log.warning("Auto-tag to trip failed: %s", e)

    return contact_id


def search_contacts(user_id: str, query: str, limit: int = 10) -> list:
    """Full-text search using search_vector (Postgres tsvector + GIN index).

    supabase-py's text_search() returns a builder that does NOT chain with
    .limit() or .range() — calling either raises AttributeError. Workaround:
    call .execute() directly, slice in Python.
    """
    if not query.strip():
        return []
    client = get_client()
    try:
        res = (
            client.table("contacts")
            .select("*")
            .eq("user_id", user_id)
            .text_search("search_vector", query, options={"type": "websearch"})
            .execute()
        )
        return (res.data or [])[:limit]
    except Exception as e:
        log.warning("text_search failed, falling back to ilike: %s", e)
        # Fallback: substring search across key fields (incl. website)
        res = (
            client.table("contacts")
            .select("*")
            .eq("user_id", user_id)
            .or_(
                f"name.ilike.%{query}%,"
                f"handle.ilike.%{query}%,"
                f"company.ilike.%{query}%,"
                f"title.ilike.%{query}%,"
                f"notes.ilike.%{query}%,"
                f"website.ilike.%{query}%,"
                f"email.ilike.%{query}%"
            )
            .execute()
        )
        return (res.data or [])[:limit] or []


def update_contact_notes(contact_id: str, user_id: str, new_note: str, append: bool = True) -> bool:
    """Append or replace notes on a contact. Returns True if successful."""
    client = get_client()
    if append:
        existing = (
            client.table("contacts")
            .select("notes")
            .eq("id", contact_id)
            .eq("user_id", user_id)
            .maybe_single()
            .execute()
        )
        prev = (existing.data or {}).get("notes") or ""
        new_notes = (prev + "\n" + new_note).strip() if prev else new_note
    else:
        new_notes = new_note
    result = (
        client.table("contacts")
        .update({"notes": new_notes})
        .eq("id", contact_id)
        .eq("user_id", user_id)
        .execute()
    )
    return bool(result.data)


def find_contacts_by_name(user_id: str, name: str) -> list:
    """Get all contacts with a given name (case-insensitive partial). For disambiguation."""
    client = get_client()
    res = (
        client.table("contacts")
        .select("*")
        .eq("user_id", user_id)
        .ilike("name", f"%{name}%")
        .execute()
    )
    return res.data or []


def update_contact_field(contact_id: str, user_id: str, field: str, value: str) -> bool:
    """Update a single field on a contact. Used by AI for edits like 'change his company to X'."""
    client = get_client()
    allowed = {"name", "handle", "company", "title", "email", "phone", "website", "notes"}
    if field not in allowed:
        raise ValueError(f"Field {field!r} not editable. Allowed: {sorted(allowed)}")
    result = (
        client.table("contacts")
        .update({field: value})
        .eq("id", contact_id)
        .eq("user_id", user_id)
        .execute()
    )
    return bool(result.data)


def list_recent(user_id: str, limit: int = 10) -> list:
    try:
        client = get_client()
        res = (
            client.table("contacts")
            .select("*")
            .eq("user_id", user_id)
            .order("saved_at", desc=True)
            .limit(limit)
            .execute()
        )
        return res.data or []
    except Exception as e:
        log.warning("list_recent failed: %s", e)
        return []


def _safe(fn, default=None):
    """Run a db function, log on error, return default."""
    try:
        return fn()
    except Exception as e:
        log.warning("db call failed: %s", e)
        return default


def set_active_trip(user_id: str, event_name: str) -> str:
    """Mark all user events inactive, create/find the named event, mark active."""
    client = get_client()
    client.table("events").update({"active": False}).eq("user_id", user_id).execute()
    slug = event_name.lower().replace(" ", "_").replace("/", "-")
    res = (
        client.table("events")
        .select("id")
        .eq("user_id", user_id)
        .eq("slug", slug)
        .maybe_single()
        .execute()
    )
    if res.data:
        event_id = res.data["id"]
        client.table("events").update({"active": True}).eq("id", event_id).execute()
        return event_id
    res = client.table("events").insert({
        "user_id": user_id,
        "slug": slug,
        "name": event_name,
        "active": True,
    }).execute()
    return res.data[0]["id"]


def get_active_trip(user_id: str) -> Optional[dict]:
    # Uses .limit(1) — same pattern as list_recent, but .eq() + .eq() + .limit() chains fine
    client = get_client()
    res = (
        client.table("events")
        .select("*")
        .eq("user_id", user_id)
        .eq("active", True)
        .limit(1)
        .execute()
    )
    return res.data[0] if res.data else None


def deactivate_trip(user_id: str):
    get_client().table("events").update({"active": False}).eq("user_id", user_id).execute()


def count_trip_contacts(event_id: str) -> int:
    client = get_client()
    res = (
        client.table("contact_events")
        .select("contact_id", count="exact")
        .eq("event_id", event_id)
        .execute()
    )
    return res.count or 0


def update_contact_notes(contact_id: str, notes: str) -> bool:
    """Update notes on a contact. Returns True on success."""
    client = get_client()
    res = (
        client.table("contacts")
        .update({"notes": notes})
        .eq("id", contact_id)
        .execute()
    )
    return bool(res.data)


def get_filtered_contacts(user_id: str, filter_type: str, filter_value: str) -> list:
    """For /send command. Returns [{id, name, handle, telegram_user_id}, ...]."""
    client = get_client()
    if filter_type == "tag":
        tag_res = (
            client.table("tags")
            .select("id")
            .eq("user_id", user_id)
            .eq("name", filter_value)
            .maybe_single()
            .execute()
        )
        if not tag_res.data:
            return []
        tag_id = tag_res.data["id"]
        res = (
            client.table("contact_tags")
            .select("contact_id, contacts!inner(id, name, handle, telegram_user_id, user_id)")
            .eq("tag_id", tag_id)
            .eq("contacts.user_id", user_id)
            .execute()
        )
        return [
            {
                "id": r["contacts"]["id"],
                "name": r["contacts"]["name"],
                "handle": r["contacts"]["handle"],
                "telegram_user_id": r["contacts"]["telegram_user_id"],
            }
            for r in (res.data or [])
            if r["contacts"].get("handle")
        ]
    elif filter_type in ("event", "trip"):
        slug = filter_value.lower().replace(" ", "_")
        evt_res = (
            client.table("events")
            .select("id")
            .eq("user_id", user_id)
            .or_(f"name.ilike.%{filter_value}%,slug.eq.{slug}")
            .limit(1)
            .execute()
        )
        if not evt_res.data:
            return []
        event_id = evt_res.data[0]["id"]
        res = (
            client.table("contact_events")
            .select("contact_id, contacts!inner(id, name, handle, telegram_user_id, user_id)")
            .eq("event_id", event_id)
            .eq("contacts.user_id", user_id)
            .execute()
        )
        return [
            {
                "id": r["contacts"]["id"],
                "name": r["contacts"]["name"],
                "handle": r["contacts"]["handle"],
                "telegram_user_id": r["contacts"]["telegram_user_id"],
            }
            for r in (res.data or [])
            if r["contacts"].get("handle")
        ]
    elif filter_type == "all":
        res = (
            client.table("contacts")
            .select("id, name, handle, telegram_user_id")
            .eq("user_id", user_id)
            .not_.is_("handle", "null")
            .order("name")
            .execute()
        )
        return res.data or []
    return []


if __name__ == "__main__":
    init_db()
    print("Supabase DB layer OK")


# ============================================================================
# Reminders
# ============================================================================

def get_user_timezone(user_id: str) -> Optional[str]:
    """Return the user's IANA timezone string, or None if not set."""
    try:
        client = get_client()
        res = (
            client.table("users")
            .select("timezone")
            .eq("id", user_id)
            .limit(1)
            .execute()
        )
        if res.data and len(res.data) > 0:
            return res.data[0].get("timezone")
        return None
    except Exception as e:
        log.warning("get_user_timezone failed: %s", e)
        return None


def set_user_timezone(user_id: str, timezone: str) -> bool:
    """Persist the user's IANA timezone (e.g. 'Asia/Hong_Kong')."""
    try:
        client = get_client()
        client.table("users").update({"timezone": timezone}).eq("id", user_id).execute()
        return True
    except Exception as e:
        log.warning("set_user_timezone failed: %s", e)
        return False


def set_reminder(
    user_id: str,
    message: str,
    due_at_iso: str,
    timezone: str,
    contact_id: Optional[str] = None,
    recurrence: str = "none",
    recurrence_end: Optional[str] = None,
    parent_id: Optional[str] = None,
) -> Optional[str]:
    """Create a reminder. Returns the reminder UUID or None on failure.

    Args:
        due_at_iso: ISO8601 with offset, e.g. '2026-06-24T10:00:00+08:00'
        recurrence: 'none' | 'daily' | 'weekly' | 'monthly'
        recurrence_end: 'YYYY-MM-DD' or None for no end
        parent_id: for child firings of a recurring series
    """
    try:
        client = get_client()
        payload = {
            "user_id": user_id,
            "contact_id": contact_id,
            "message": message,
            "due_at": due_at_iso,
            "timezone": timezone,
            "status": "pending",
            "recurrence": recurrence,
        }
        if recurrence_end:
            payload["recurrence_end"] = recurrence_end
        if parent_id:
            payload["parent_id"] = parent_id
        res = client.table("reminders").insert(payload).execute()
        if res.data and len(res.data) > 0:
            return res.data[0]["id"]
        return None
    except Exception as e:
        log.warning("set_reminder failed: %s", e)
        return None


def list_reminders(user_id: str, status: str = "pending", limit: int = 20) -> list:
    """List reminders for a user, optionally filtered by status."""
    try:
        client = get_client()
        q = (
            client.table("reminders")
            .select("*, contact:contacts(id, name, handle, company)")
            .eq("user_id", user_id)
        )
        if status != "all":
            q = q.eq("status", status)
        res = (
            q.order("due_at", desc=False)
            .limit(limit)
            .execute()
        )
        return res.data or []
    except Exception as e:
        log.warning("list_reminders failed: %s", e)
        return []


def get_due_reminders(limit: int = 100) -> list:
    """Return all pending reminders whose due_at <= now. Called by scheduler."""
    try:
        client = get_client()
        # Use RPC or raw filter — Supabase Python supports lt on timestamps
        from datetime import datetime, timezone
        now_iso = datetime.now(timezone.utc).isoformat()
        res = (
            client.table("reminders")
            .select("*, user:users(id, telegram_user_id, timezone), "
                    "contact:contacts(id, name, handle, company, email)")
            .eq("status", "pending")
            .lte("due_at", now_iso)
            .limit(limit)
            .execute()
        )
        return res.data or []
    except Exception as e:
        log.warning("get_due_reminders failed: %s", e)
        return []


def mark_reminder_fired(reminder_id: str) -> Optional[str]:
    """Mark a one-shot reminder as done. For recurring, return the new
    due_at ISO string for the next occurrence (or None if series ended)."""
    try:
        client = get_client()
        # Fetch the reminder
        res = (
            client.table("reminders")
            .select("*")
            .eq("id", reminder_id)
            .limit(1)
            .execute()
        )
        if not res.data or len(res.data) == 0:
            return None
        r = res.data[0]

        if r.get("recurrence", "none") == "none":
            client.table("reminders").update({
                "status": "done",
                "fired_at": "now()",
                "last_fired_at": "now()",
            }).eq("id", reminder_id).execute()
            return None

        # Recurring: compute next occurrence
        from datetime import datetime, timedelta
        from dateutil.relativedelta import relativedelta
        current_due = datetime.fromisoformat(r["due_at"].replace("Z", "+00:00"))

        if r["recurrence"] == "daily":
            next_due = current_due + timedelta(days=1)
        elif r["recurrence"] == "weekly":
            next_due = current_due + timedelta(weeks=1)
        elif r["recurrence"] == "monthly":
            next_due = current_due + relativedelta(months=1)
        else:
            next_due = current_due

        # Check recurrence_end
        if r.get("recurrence_end"):
            end_date = datetime.fromisoformat(r["recurrence_end"]).date()
            if next_due.date() > end_date:
                client.table("reminders").update({
                    "status": "done",
                    "fired_at": "now()",
                    "last_fired_at": "now()",
                }).eq("id", reminder_id).execute()
                return None

        # Advance due_at to next occurrence
        client.table("reminders").update({
            "due_at": next_due.isoformat(),
            "fired_at": "now()",
            "last_fired_at": "now()",
        }).eq("id", reminder_id).execute()
        return next_due.isoformat()

    except Exception as e:
        log.warning("mark_reminder_fired failed: %s", e)
        return None


def cancel_reminder(reminder_id: str, user_id: str) -> bool:
    """Cancel a reminder (mark status=cancelled). User-scoped."""
    try:
        client = get_client()
        res = (
            client.table("reminders")
            .update({"status": "cancelled"})
            .eq("id", reminder_id)
            .eq("user_id", user_id)
            .execute()
        )
        return bool(res.data)
    except Exception as e:
        log.warning("cancel_reminder failed: %s", e)
        return False


def snooze_reminder(reminder_id: str, user_id: str, minutes: int) -> bool:
    """Push a reminder's due_at forward by N minutes."""
    try:
        client = get_client()
        # Fetch current due_at
        res = (
            client.table("reminders")
            .select("due_at")
            .eq("id", reminder_id)
            .eq("user_id", user_id)
            .limit(1)
            .execute()
        )
        if not res.data or len(res.data) == 0:
            return False
        from datetime import datetime, timedelta
        current = datetime.fromisoformat(res.data[0]["due_at"].replace("Z", "+00:00"))
        new_due = current + timedelta(minutes=minutes)
        client.table("reminders").update({
            "due_at": new_due.isoformat(),
            "status": "pending",
        }).eq("id", reminder_id).eq("user_id", user_id).execute()
        return True
    except Exception as e:
        log.warning("snooze_reminder failed: %s", e)
        return False


def find_reminders_by_keyword(user_id: str, keyword: str) -> list:
    """Find pending reminders whose message contains the keyword (for 'cancel the Vitalik one')."""
    try:
        client = get_client()
        res = (
            client.table("reminders")
            .select("*")
            .eq("user_id", user_id)
            .eq("status", "pending")
            .ilike("message", f"%{keyword}%")
            .execute()
        )
        return res.data or []
    except Exception as e:
        log.warning("find_reminders_by_keyword failed: %s", e)
        return []


def find_contact_by_partial_name(user_id: str, name: str) -> Optional[dict]:
    """Find a single contact matching a partial name (for reminder contact linking)."""
    try:
        client = get_client()
        res = (
            client.table("contacts")
            .select("*")
            .eq("user_id", user_id)
            .ilike("name", f"%{name}%")
            .limit(1)
            .execute()
        )
        if res.data and len(res.data) > 0:
            return res.data[0]
        return None
    except Exception as e:
        log.warning("find_contact_by_partial_name failed: %s", e)
        return None


# ---------------------------------------------------------------------------
# Chat history — persistent per-user conversation context
# ---------------------------------------------------------------------------
# The bot's LLM has no built-in memory. We persist user/assistant turns to
# Supabase and load the last N before each LLM call so the AI can see what
# the user just said (and not re-ask for it). See migration 004.

def save_message(user_id: str, role: str, content: str = None) -> None:
    """Persist a single chat message. Failures MUST NOT break a real command.

    role: 'user' | 'assistant' | 'tool'
    """
    try:
        row = {"user_id": user_id, "role": role}
        if content is not None:
            row["content"] = content
        get_client().table("messages").insert(row).execute()
    except Exception as e:
        log.warning("save_message failed: %s", e)


def get_recent_messages(user_id: str, limit: int = 10) -> list:
    """Return the last N user/assistant messages for a user, oldest first.

    Returns a list of dicts in OpenAI messages format:
        [{"role": "user", "content": "..."}, {"role": "assistant", "content": "..."}]
    Drops 'tool' rows — the AI re-runs its own tool calls each turn, so
    replaying tool_call_id metadata would just confuse it.
    Empty list on failure (caller treats as 'no history').
    """
    try:
        res = (
            get_client().table("messages")
            .select("role, content, created_at")
            .eq("user_id", user_id)
            .order("created_at", desc=True)
            .limit(limit)
            .execute()
        )
        # Reverse to chronological (oldest first), drop tool rows + empties
        rows = list(reversed(res.data or []))
        return [
            {"role": r["role"], "content": r["content"]}
            for r in rows
            if r.get("content") and r["role"] in ("user", "assistant")
        ]
    except Exception as e:
        log.warning("get_recent_messages failed: %s", e)
        return []


def prune_old_messages(user_id: str, keep: int = 50) -> int:
    """Optional: trim a user's history to the last `keep` messages.

    Returns count of rows deleted. Not used by the bot itself — only for
    the eventual cron job or a manual cleanup. Safe to call; non-fatal on error.
    """
    try:
        client = get_client()
        # Find the cutoff created_at
        cutoff_res = (
            client.table("messages")
            .select("created_at")
            .eq("user_id", user_id)
            .order("created_at", desc=True)
            .offset(keep)
            .limit(1)
            .execute()
        )
        if not cutoff_res.data:
            return 0
        cutoff = cutoff_res.data[0]["created_at"]
        del_res = (
            client.table("messages")
            .delete()
            .eq("user_id", user_id)
            .lt("created_at", cutoff)
            .execute()
        )
        return len(del_res.data or [])
    except Exception as e:
        log.warning("prune_old_messages failed: %s", e)
        return 0


# ---------------------------------------------------------------------------
# Auth + tier system (migration 005)
# ---------------------------------------------------------------------------
# Goal: link telegram identity to a verified email so we have a real user
# metric for funding, and gate free users at 10 contacts while grandfathering
# the 2-3 testers as unlimited.

FREE_CONTACT_LIMIT = 10


def get_user(user_id: str) -> Optional[dict]:
    """Get full user record by ID. Used for tier / limit checks."""
    try:
        res = (
            get_client().table("users")
            .select("*")
            .eq("id", user_id)
            .limit(1)
            .execute()
        )
        return res.data[0] if res.data else None
    except Exception as e:
        log.warning("get_user failed: %s", e)
        return None


def count_user_contacts(user_id: str) -> int:
    """Count all contacts for a user (regardless of status)."""
    try:
        res = (
            get_client().table("contacts")
            .select("id", count="exact")
            .eq("user_id", user_id)
            .limit(0)
            .execute()
        )
        return int(res.count or 0)
    except Exception as e:
        log.warning("count_user_contacts failed: %s", e)
        return 0


def check_contact_limit(user_id: str) -> dict:
    """Check whether a user can save another contact under their plan.

    Returns:
        {"allowed": True, "plan": "free", "current": N, "limit": 10} OR
        {"allowed": True, "plan": "pro"|"tester"} OR
        {"allowed": False, "plan": "free", "current": 10, "limit": 10, "reason": "free_limit"}

    Fails OPEN on any error — never blocks a real command because of a DB hiccup.
    """
    try:
        user = get_user(user_id)
        if not user:
            return {"allowed": True}

        plan = user.get("plan", "free")
        is_tester = bool(user.get("is_tester", False))

        # Unlimited tiers bypass the count check
        if plan in ("pro", "team", "tester") or is_tester:
            return {"allowed": True, "plan": "tester" if is_tester else plan}

        # Free plan: count and compare
        current = count_user_contacts(user_id)
        if current >= FREE_CONTACT_LIMIT:
            return {
                "allowed": False,
                "plan": "free",
                "current": current,
                "limit": FREE_CONTACT_LIMIT,
                "reason": "free_limit",
            }
        return {
            "allowed": True,
            "plan": "free",
            "current": current,
            "limit": FREE_CONTACT_LIMIT,
        }
    except Exception as e:
        log.warning("check_contact_limit failed: %s", e)
        return {"allowed": True}


def create_magic_token(user_id: str, email: str, token: str) -> bool:
    """Create a magic-link token. Wipes any prior un-used tokens for this user."""
    try:
        client = get_client()
        client.table("magic_link_tokens").delete().eq("user_id", user_id).execute()
        client.table("magic_link_tokens").insert({
            "user_id": user_id,
            "email": email,
            "token": token,
        }).execute()
        return True
    except Exception as e:
        log.warning("create_magic_token failed: %s", e)
        return False


def consume_magic_token(token: str) -> dict:
    """Consume a magic-link token. Marks used_at + sets the user's email_verified.

    Returns: {"success": True, "email": "..."} | {"error": "..."}
    """
    try:
        client = get_client()
        res = (
            client.table("magic_link_tokens")
            .select("*")
            .eq("token", token)
            .limit(1)
            .execute()
        )
        if not res.data:
            return {"error": "Invalid link. Try /signup again."}
        record = res.data[0]

        from datetime import datetime, timezone
        expires_at = datetime.fromisoformat(record["expires_at"].replace("Z", "+00:00"))
        if expires_at < datetime.now(timezone.utc):
            return {"error": "Link expired. Try /signup again."}
        if record.get("used_at"):
            return {"error": "Link already used."}

        client.table("magic_link_tokens").update({
            "used_at": "now()",
        }).eq("token", token).execute()

        client.table("users").update({
            "email": record["email"],
            "email_verified": True,
        }).eq("id", record["user_id"]).execute()

        log.info("Magic link consumed for user_id=%s email=%s", record["user_id"], record["email"])
        return {"success": True, "email": record["email"]}
    except Exception as e:
        log.exception("consume_magic_token failed: %s", e)
        return {"error": "Verification failed. Try /signup again."}


# ---------------------------------------------------------------------------
# Stats helpers for /stats command (founder visibility)
# ---------------------------------------------------------------------------

def _count_rows(table: str, since: Optional[str] = None) -> int:
    """Count rows in a table. since=ISO timestamp for 'since X' filtering."""
    try:
        client = get_client()
        q = client.table(table).select("id", count="exact").limit(1)
        if since:
            q = q.gte("created_at", since)
        resp = q.execute()
        return resp.count or 0
    except Exception as e:
        log.exception("_count_rows(%s) failed: %s", table, e)
        return -1


def get_founder_stats() -> dict:
    """
    Read-only summary for the /stats command. Returns:
      - users_total, users_signed_up (email_verified=true)
      - contacts_total, contacts_today
      - signups_today, signups_last_7d
      - events_today, events_last_7d
      - active_trips (count of users with active trip)
      - magic_links_pending (unconsumed tokens)

    Safe to call any time — never raises, returns -1 on error.
    """
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    seven_days_ago = (now - timedelta(days=7)).isoformat()

    users_total = _count_rows("users")
    contacts_total = _count_rows("contacts")
    contacts_today = _count_rows("contacts", today_start)

    # Signed-up users (those who completed magic link)
    try:
        client = get_client()
        signed_up = client.table("users").select("id", count="exact") \
            .eq("email_verified", True).limit(1).execute().count or 0
    except Exception:
        signed_up = -1

    signups_today = _count_rows("users", today_start)
    signups_7d = _count_rows("users", seven_days_ago)

    events_today = _count_rows("events", today_start)
    events_7d = _count_rows("events", seven_days_ago)

    # Active trips
    try:
        client = get_client()
        active_trips = client.table("events").select("id", count="exact") \
            .eq("event_name", "trip_start").limit(1).execute().count or 0
    except Exception:
        active_trips = -1

    # Pending magic link tokens (unconsumed)
    try:
        client = get_client()
        pending_links = client.table("magic_link_tokens").select("id", count="exact") \
            .is_("consumed_at", "null").limit(1).execute().count or 0
    except Exception:
        pending_links = -1

    return {
        "users_total": users_total,
        "users_signed_up": signed_up,
        "contacts_total": contacts_total,
        "contacts_today": contacts_today,
        "signups_today": signups_today,
        "signups_last_7d": signups_7d,
        "events_today": events_today,
        "events_last_7d": events_7d,
        "active_trips": active_trips,
        "magic_links_pending": pending_links,
        "generated_at": now.isoformat(),
    }


# ============================================================
# Password auth (web dashboard)
# ============================================================

def set_password(user_id: str, password: str) -> dict:
    """Hash a password with bcrypt and store it on the user row.

    Called from the bot after the magic-link confirm, and from /setpassword
    for existing magic-link-only users.

    Returns {"success": True} or {"error": "..."}.
    Never raises — password failures must NEVER break the bot command.
    """
    if not password or len(password) < 8:
        return {"error": "Password must be at least 8 characters."}
    if len(password) > 128:
        return {"error": "Password too long (max 128 chars)."}
    try:
        salt = bcrypt.gensalt(rounds=BCRYPT_ROUNDS)
        password_hash = bcrypt.hashpw(password.encode("utf-8"), salt).decode("utf-8")
        client = get_client()
        client.table("users").update({
            "password_hash": password_hash,
            "password_set_at": "now()",
        }).eq("id", user_id).execute()
        return {"success": True}
    except Exception as e:
        log.warning("set_password failed for user_id=%s: %s", user_id, e)
        return {"error": "Couldn't save password. Try again."}


def verify_user_password(email: str, password: str) -> dict:
    """Look up user by email, bcrypt-verify password.

    Used by the web /login route at trce.io. Returns the internal user_id so the
    Flask session can store it.

    Returns {"success": True, "user_id": "..."} | {"error": "..."}.
    Generic error messages — don't leak whether the email exists.
    """
    try:
        email = (email or "").strip().lower()
        if not email or "@" not in email or not password:
            return {"error": "Invalid email or password."}
        client = get_client()
        res = (
            client.table("users")
            .select("id, password_hash, email_verified")
            .eq("email", email)
            .limit(1)
            .execute()
        )
        if not res.data:
            # Constant-time-ish: still hash a dummy so timing doesn't leak
            # whether the email exists.
            bcrypt.hashpw(b"dummy", bcrypt.gensalt(rounds=BCRYPT_ROUNDS))
            return {"error": "Invalid email or password."}
        row = res.data[0]
        if not row.get("password_hash") or not row.get("email_verified"):
            return {"error": "Account not set up for web login yet. Set a password first via /setpassword in the Telegram bot."}
        if bcrypt.checkpw(password.encode("utf-8"), row["password_hash"].encode("utf-8")):
            return {"success": True, "user_id": row["id"]}
        return {"error": "Invalid email or password."}
    except Exception as e:
        log.warning("verify_user_password failed: %s", e)
        return {"error": "Login failed. Try again."}


# ---------------------------------------------------------------------------
# Password reset (web /forgot + /reset/<token> flow)
# ---------------------------------------------------------------------------
# Tokens are 32-byte urlsafe random strings (secrets.token_urlsafe(32)).
# One-time use, expire 1 hour after creation. Raw string stored in DB —
# the token itself is the secret. Never log it.

PASSWORD_RESET_TTL_SECONDS = 3600  # 1 hour


def create_password_reset_token(email: str) -> Optional[str]:
    """Create a password-reset token for the given email.

    Returns the token string if a matching user exists, else None.
    Caller should NOT distinguish between 'sent' and 'no such email' in
    the response — same message either way, to avoid leaking which
    emails are registered.

    Wipes any prior un-used reset tokens for the user (only one active
    reset at a time per user).
    """
    try:
        email = (email or "").strip().lower()
        if not email or "@" not in email:
            return None
        client = get_client()
        # Look up user
        res = (
            client.table("users")
            .select("id")
            .eq("email", email)
            .limit(1)
            .execute()
        )
        if not res.data:
            return None
        user_id = res.data[0]["id"]
        # Wipe prior un-used tokens for this user
        client.table("password_reset_tokens").delete().eq(
            "user_id", user_id
        ).is_("used_at", "null").execute()
        # Generate new token
        import secrets
        token = secrets.token_urlsafe(32)
        expires_at = datetime.now(timezone.utc) + timedelta(
            seconds=PASSWORD_RESET_TTL_SECONDS
        )
        client.table("password_reset_tokens").insert({
            "user_id": user_id,
            "token": token,
            "expires_at": expires_at.isoformat(),
        }).execute()
        return token
    except Exception as e:
        log.warning("create_password_reset_token failed: %s", e)
        return None


def consume_password_reset_token(token: str, new_password: str) -> dict:
    """Validate a reset token and set a new password.

    Returns {"success": True, "email": "..."} | {"error": "..."}.
    Never raises — login flow must not crash.

    On success: marks token used, hashes new_password with bcrypt, updates
    users.password_hash + password_set_at, returns the email so the caller
    can show 'log in with your new password' confirmation.
    """
    try:
        if not token or not new_password or len(new_password) < 8:
            return {"error": "Invalid link or password (must be 8+ characters)."}
        client = get_client()
        # Look up token (without marking used yet)
        res = (
            client.table("password_reset_tokens")
            .select("id, user_id, expires_at, used_at")
            .eq("token", token)
            .limit(1)
            .execute()
        )
        if not res.data:
            return {"error": "This reset link is invalid. Request a new one."}
        row = res.data[0]
        if row.get("used_at"):
            return {"error": "This reset link has already been used."}
        if datetime.fromisoformat(row["expires_at"].replace("Z", "+00:00")) < datetime.now(timezone.utc):
            return {"error": "This reset link has expired. Request a new one."}
        # Hash the new password using the existing set_password helper
        result = set_password(row["user_id"], new_password)
        if "error" in result:
            return result
        # Mark token used
        client.table("password_reset_tokens").update({
            "used_at": "now()",
        }).eq("id", row["id"]).execute()
        # Return the user's email for the success page
        user_res = client.table("users").select("email").eq("id", row["user_id"]).limit(1).execute()
        email = user_res.data[0]["email"] if user_res.data else None
        return {"success": True, "email": email}
    except Exception as e:
        log.warning("consume_password_reset_token failed: %s", e)
        return {"error": "Couldn't reset password. Try again."}


def has_password(user_id: str) -> bool:
    """True if this user has set a password (web dashboard access enabled)."""
    try:
        res = (
            get_client().table("users")
            .select("password_hash")
            .eq("id", user_id)
            .limit(1)
            .execute()
        )
        return bool(res.data and res.data[0].get("password_hash"))
    except Exception:
        return False


def set_user_plan(
    user_id: str,
    plan: str,
    *,
    stripe_customer_id: Optional[str] = None,
    stripe_subscription_id: Optional[str] = None,
    subscription_status: Optional[str] = None,
    subscription_period_end: Optional[str] = None,
    pro_since: Optional[str] = None,
    cancel_at_period_end: Optional[bool] = None,
) -> bool:
    """Set a user's plan tier + optional Stripe identifiers. Idempotent.

    Called by the Stripe webhook handler when:
      - checkout.session.completed         -> plan='pro', pro_since=now
      - customer.subscription.updated      -> plan='pro' if active else 'free'
      - customer.subscription.deleted      -> plan='free', clear subscription_id
      - invoice.payment_failed             -> subscription_status='past_due'

    `plan` must be one of 'free' | 'pro' | 'team' | 'tester'.
    Returns True on success, False on any DB error. Never raises (webhook safety).

    Pass stripe_customer_id/stripe_subscription_id/subscription_status to
    update those columns in the same row (atomic). pro_since sets the
    `pro_since` timestamp the first time a user goes pro -- pass it on the
    checkout.session.completed call only, leave None on renewals.

    `cancel_at_period_end` distinguishes 'active until period ends' (user
    canceled via portal but kept access) from 'active normal' — used by the
    dashboard to show "Pro ends [date]" banner.
    """
    if plan not in ("free", "pro", "team", "tester"):
        log.warning("set_user_plan: invalid plan %r for user %s", plan, user_id)
        return False
    try:
        client = get_client()
        update = {"plan": plan}
        if stripe_customer_id is not None:
            update["stripe_customer_id"] = stripe_customer_id
        if stripe_subscription_id is not None:
            update["stripe_subscription_id"] = stripe_subscription_id
        if subscription_status is not None:
            update["subscription_status"] = subscription_status
        if subscription_period_end is not None:
            update["subscription_period_end"] = subscription_period_end
        if pro_since is not None:
            update["pro_since"] = pro_since
        if cancel_at_period_end is not None:
            update["cancel_at_period_end"] = cancel_at_period_end
        client.table("users").update(update).eq("id", user_id).execute()
        return True
    except Exception as e:
        log.warning("set_user_plan failed for user=%s plan=%s: %s", user_id, plan, e)
        return False


def set_user_tester(target_user_id: str, is_tester: bool) -> dict:
    """Toggle the tester flag on a user (founder-only via /tester bot command).

    Sets is_tester=True AND plan='tester' so check_contact_limit() sees them as
    unlimited. The target_user_id is the INTERNAL users.id UUID, not the
    Telegram user_id. Use get_or_create_user(telegram_user_id) first to resolve.

    Returns {"success": True, "email": "..."} or {"error": "..."}.
    Never raises.
    """
    try:
        client = get_client()
        res = client.table("users").select("email").eq("id", target_user_id).limit(1).execute()
        if not res.data:
            return {"error": "User not found. They need to /start the bot first."}
        update = {
            "is_tester": is_tester,
        }
        if is_tester:
            update["plan"] = "tester"
            update["email_verified"] = True  # testers skip the magic-link step
        # When un-testering, demote them to 'free' (10-contact cap resumes).
        elif not is_tester:
            update["plan"] = "free"
        client.table("users").update(update).eq("id", target_user_id).execute()
        return {"success": True, "email": res.data[0].get("email")}
    except Exception as e:
        log.warning("set_user_tester failed: %s", e)
        return {"error": f"DB error: {e}"}


def find_user_by_username(username: str) -> Optional[dict]:
    """Look up a user by their Telegram @username (without the @).

    Returns the row or None. Use for /tester @friend lookup.
    """
    try:
        u = username.lstrip("@").lower()
        if not u:
            return None
        res = (
            get_client().table("users")
            .select("id, telegram_user_id, telegram_username, email, plan, is_tester")
            .ilike("telegram_username", u)
            .limit(1)
            .execute()
        )
        return res.data[0] if res.data else None
    except Exception as e:
        log.warning("find_user_by_username failed: %s", e)
        return None


def find_user_by_telegram_id(tg_id: int) -> Optional[dict]:
    """Look up a user by their numeric Telegram user_id (NOT the internal UUID)."""
    try:
        res = (
            get_client().table("users")
            .select("id, telegram_user_id, telegram_username, email, plan, is_tester")
            .eq("telegram_user_id", tg_id)
            .limit(1)
            .execute()
        )
        return res.data[0] if res.data else None
    except Exception as e:
        log.warning("find_user_by_telegram_id failed: %s", e)
        return None


def get_user_by_id(user_id: str) -> Optional[dict]:
    """Look up a user row by internal UUID. Used by the web dashboard to resolve
    the session user_id back to email + plan.
    Returns dict or None.
    Note: includes stripe_customer_id so /billing-portal can find the Stripe
    customer without a second round-trip. Don't add sensitive fields here."""
    try:
        res = (
            get_client().table("users")
            .select("id, email, email_verified, plan, is_tester, display_name, "
                    "stripe_customer_id, subscription_status, stripe_subscription_id, "
                    "subscription_period_end, cancel_at_period_end")
            .eq("id", user_id)
            .limit(1)
            .execute()
        )
        return res.data[0] if res.data else None
    except Exception:
        return None


def list_user_contacts(user_id: str, limit: int = 500) -> list:
    """All contacts for a user, newest first. Used by the web dashboard list view.

    Returns up to `limit` rows with the editable fields the dashboard exposes.
    Cap at 500 by default — soft limit for the MVP, real users shouldn't hit it.
    """
    try:
        res = (
            get_client().table("contacts")
            .select("id, name, handle, company, title, email, phone, website, notes, source, saved_at, last_contacted_at")
            .eq("user_id", user_id)
            .order("saved_at", desc=True)
            .limit(limit)
            .execute()
        )
        return res.data or []
    except Exception as e:
        log.warning("list_user_contacts failed: %s", e)
        return []


def delete_contact(contact_id: str, user_id: str) -> bool:
    """Hard-delete a contact. **Web dashboard only — never call from the bot.**

    Defensive: filters by BOTH contact_id AND user_id so a user can only delete
    their own contacts. The RLS policies on `contacts` table also enforce this,
    but the user_id filter here is belt-and-suspenders.

    Why dashboard-only: the bot is conversational, and accidentally deleting
    data via "delete John" / "remove that contact" / typo is too easy. Going
    through the dashboard forces a deliberate click on a real button.

    Returns True on success, False otherwise. Never raises — caller logs.
    """
    try:
        res = (
            get_client().table("contacts")
            .delete()
            .eq("id", contact_id)
            .eq("user_id", user_id)
            .execute()
        )
        return bool(res.data)
    except Exception as e:
        log.warning("delete_contact failed for user_id=%s contact_id=%s: %s", user_id, contact_id, e)
        return False
