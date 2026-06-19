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
from supabase import create_client, Client

log = logging.getLogger(__name__)

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
