"""Reminder input parsing via M3.

Converts natural-language /remind input into a structured {when, what, recurrence}
payload that set_reminder() can persist. Examples:

  /remind tomorrow at 10am call Vitalik
  /remind next Tuesday morning follow up with Alex about the deck
  /remind in 5 minutes ping me to call Mike
  /remind every monday at 9am check in with investors
  /remind 2026-09-15 14:00 send TOKEN2049 pitch deck

Returns:
  {
    "due_at_iso": "2026-09-02T10:00:00+08:00",
    "due_at_human": "tomorrow at 10am HKT",
    "message": "call Vitalik",
    "recurrence": "none" | "daily" | "weekly" | "monthly",
    "recurrence_end": null,
    "now_in_tz": "2026-09-01T17:54:00+08:00",  # for the model to ground 'today'
    "tz_error": null,
  }
"""

import asyncio
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

import httpx

from ai import MINIMAX_API_KEY, MINIMAX_MODEL, MINIMAX_BASE_URL

log = logging.getLogger(__name__)

RECURRENCE_VALUES = ("none", "daily", "weekly", "monthly")

SYSTEM_PROMPT = """You extract a reminder from natural-language input.

Output STRICT JSON only (no prose, no markdown) with these fields:
- when_iso: ISO8601 with timezone offset (e.g. '2026-09-02T10:00:00+08:00'). Use the user's timezone unless input specifies another.
- due_at_human: human-readable form of the date/time in the user's timezone, e.g. 'tomorrow at 10am HKT' or 'next Tuesday 9am'
- message: the action/reminder text, stripped of the time phrase. e.g. for 'tomorrow at 10am call Vitalik' -> 'call Vitalik'. Keep it short.
- recurrence: 'none' for one-shot, 'daily', 'weekly', or 'monthly' if user says 'every X'
- recurrence_end: YYYY-MM-DD or null

Grounding rules:
- 'tomorrow' = date + 1 day at 09:00 (default time)
- 'today' = today's date at 09:00 (if said in the morning) or current time + 1 hour (if said later)
- 'next monday' = the upcoming Monday (in user's timezone)
- 'in N minutes/hours/days' = now + N units
- 'noon' = 12:00, 'midnight' = 00:00 of next day, 'morning' = 09:00, 'afternoon' = 14:00, 'evening' = 18:00
- '10am' or '10:00' = 10:00 same day (or next business day if already past)
- If multiple times mentioned, pick the EARLIEST one

The 'now_in_tz' field gives you the current time in the user's timezone for grounding.

Output ONLY the JSON object."""


async def parse_reminder(user_input: str, tz_name: str = "Asia/Hong_Kong") -> Optional[dict]:
    """Run M3 with thinking disabled to extract {when_iso, message, recurrence}.

    Returns dict with all fields, or None if parse failed.
    On parse failure, returns a dict with error info so caller can show a friendly message.
    """
    if not MINIMAX_API_KEY:
        return {"error": "MINIMAX_API_KEY not configured"}

    # Compute 'now' in user's timezone for grounding
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo("Asia/Hong_Kong")
    now = datetime.now(tz)
    now_iso = now.strftime("%Y-%m-%dT%H:%M:%S%z")

    # Build the user prompt
    user_prompt = f"""Current user timezone: {tz_name}
Now in user's timezone: {now_iso}

User said:
{user_input}

Extract the reminder as JSON."""

    payload = {
        "model": MINIMAX_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.1,
        "max_tokens": 400,
        "thinking": {"type": "disabled"},  # critical for short JSON tasks
    }

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.post(
                f"{MINIMAX_BASE_URL.rstrip('/')}/chat/completions",
                headers={
                    "Authorization": f"Bearer {MINIMAX_API_KEY}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
            r.raise_for_status()
            data = r.json()
    except Exception as e:
        log.warning("parse_reminder M3 call failed: %s", e)
        return {"error": f"AI parse failed: {e}"}

    content = (data.get("choices") or [{}])[0].get("message", {}).get("content") or ""
    content = content.strip().strip("`")
    # Strip any leaked <think> blocks
    content = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL).strip()

    # Extract JSON from the response (model sometimes wraps in ```json ... ```)
    json_match = re.search(r"\{.*\}", content, flags=re.DOTALL)
    if not json_match:
        return {"error": f"Couldn't parse AI response: {content[:200]}"}

    try:
        parsed = json.loads(json_match.group(0))
    except json.JSONDecodeError as e:
        return {"error": f"Invalid JSON from AI: {e}\nRaw: {content[:200]}"}

    # Validate required fields
    when_iso = parsed.get("when_iso", "")
    message = parsed.get("message", "").strip()
    recurrence = parsed.get("recurrence", "none")

    if not when_iso:
        return {"error": "AI didn't return a when_iso"}
    if not message:
        return {"error": "AI didn't return a message"}
    if recurrence not in RECURRENCE_VALUES:
        recurrence = "none"

    # Validate due_at parses
    try:
        due_dt = datetime.fromisoformat(when_iso)
        if due_dt.tzinfo is None:
            # M3 sometimes drops the offset — reattach user's tz
            due_dt = due_dt.replace(tzinfo=tz)
            when_iso = due_dt.isoformat()
    except ValueError as e:
        return {"error": f"Invalid date format: {when_iso} ({e})"}

    return {
        "due_at_iso": when_iso,
        "due_at_human": parsed.get("due_at_human", when_iso),
        "message": message,
        "recurrence": recurrence,
        "recurrence_end": parsed.get("recurrence_end"),
        "tz_name": tz_name,
        "now_in_tz": now_iso,
    }
