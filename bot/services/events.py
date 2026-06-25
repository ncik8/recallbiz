"""events.py — non-blocking analytics logger for TRCE bot.

Mirrors the rezmycv pattern: log_event() is safe to call anywhere (never
raises), writes happen on a module-level ThreadPoolExecutor so they survive
across the request/task in async bot code, failures are logged not swallowed.

Why ThreadPoolExecutor (and not asyncio.create_task)?
  - We want the write to complete even if the bot task finishes (gunicorn /
    event loop returning).
  - ThreadPoolExecutor survives across async tasks within the process.
  - Submit returns immediately so the bot handler is never blocked.

For the bot, we wrap the call in run_in_executor at the call site OR
call sync from a sync handler. Either way the executor does the work.
"""
import atexit
import json
import logging
from concurrent.futures import ThreadPoolExecutor

from db import get_client as get_supabase_client

_log = logging.getLogger(__name__)

# Module-level executor — survives across bot dispatcher tasks within one
# process. 2 workers is enough because Supabase writes are quick (~200ms).
_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="trce-events")


def _write_event(payload: dict) -> None:
    """Actually do the Supabase insert. Runs on the executor."""
    try:
        client = get_supabase_client()
        client.table("analytics_events").insert(payload).execute()
    except Exception as e:
        _log.warning("[trce-events] background write failed: %s: %s | payload=%s",
                     type(e).__name__, e, payload)


@atexit.register
def _shutdown_executor(wait=True):
    """On process shutdown, give in-flight events a moment to flush."""
    _executor.shutdown(wait=wait)


def log_event(user_id, event_name: str, **properties) -> None:
    """Log an analytics event. Safe to call anywhere — never raises.

    Args:
        user_id:     The public.users UUID, or None for anonymous.
        event_name:  Short snake_case name, e.g. 'bot_start', 'first_contact_saved'.
        **properties: Arbitrary key=value pairs stored as JSONB.

    Returns immediately. Actual write happens on a background thread.
    """
    try:
        payload = {"event_name": event_name}
        if user_id:
            payload["user_id"] = user_id
        if properties:
            payload["properties"] = json.loads(json.dumps(properties, default=str))
        _executor.submit(_write_event, payload)
    except Exception as e:
        _log.warning("[trce-events] submit failed: %s: %s", type(e).__name__, e)