"""Supabase-backed DB layer for RecallBiz.

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
        # Fallback: substring search across key fields
        res = (
            client.table("contacts")
            .select("*")
            .eq("user_id", user_id)
            .or_(
                f"name.ilike.%{query}%,"
                f"handle.ilike.%{query}%,"
                f"company.ilike.%{query}%,"
                f"title.ilike.%{query}%,"
                f"notes.ilike.%{query}%"
            )
            .execute()
        )
        return (res.data or [])[:limit] or []


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
