"""MiniMax AI integration for RecallBiz bot.

Two purposes:
1. OCR — extract structured business card fields from images
2. Conversational — natural-language understanding with function calling

Uses MiniMax-M3 model. Reads MINIMAX_API_KEY from env.
"""
import os
import re
import json
import logging
import base64
from typing import Optional, List, Dict, Any

import httpx

log = logging.getLogger(__name__)

MINIMAX_API_KEY = os.environ.get("MINIMAX_API_KEY")
MINIMAX_BASE_URL = os.environ.get("MINIMAX_BASE_URL", "https://api.minimax.io/v1")
MINIMAX_CHAT_URL = os.environ.get("MINIMAX_CHAT_URL")
MINIMAX_MODEL = os.environ.get("MINIMAX_MODEL", "MiniMax-M3")


def _chat_url() -> str:
    """Resolve the chat completions URL.

    Precedence: MINIMAX_CHAT_URL > MINIMAX_BASE_URL + /chat/completions > default.
    """
    if MINIMAX_CHAT_URL:
        return MINIMAX_CHAT_URL
    return MINIMAX_BASE_URL.rstrip("/") + "/chat/completions"


async def call_minimax(
    messages: List[Dict[str, Any]],
    tools: Optional[List[Dict]] = None,
    model: Optional[str] = None,
    temperature: float = 0.3,
    timeout: float = 60.0,
) -> Dict[str, Any]:
    """Make an async chat completion call."""
    if not MINIMAX_API_KEY:
        raise RuntimeError("MINIMAX_API_KEY not set")
    headers = {
        "Authorization": f"Bearer {MINIMAX_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model or MINIMAX_MODEL,
        "messages": messages,
        "temperature": temperature,
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    async with httpx.AsyncClient(timeout=timeout) as client:
        r = await client.post(_chat_url(), headers=headers, json=payload)
        r.raise_for_status()
        return r.json()


def _parse_json_from_text(text: str) -> Optional[dict]:
    """Extract JSON object from model output (handles ```json blocks, leading prose)."""
    text = text.strip()
    # Strip code fences
    fence = re.match(r"^```(?:json)?\s*(\{.*?\})\s*```$", text, re.DOTALL)
    if fence:
        try:
            return json.loads(fence.group(1))
        except Exception:
            pass
    # First balanced { ... } block
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                candidate = text[start:i + 1]
                try:
                    return json.loads(candidate)
                except Exception:
                    return None
    return None


async def extract_card_from_image(image_bytes: bytes) -> Optional[dict]:
    """Use MiniMax vision to extract structured business card fields.

    Returns: {name, title, company, email, phone, website, handle} or None.
    Drops fields that are None/empty.
    """
    b64 = base64.b64encode(image_bytes).decode("ascii")
    messages = [
        {
            "role": "system",
            "content": (
                "You extract structured data from business card images. "
                "Return ONLY a JSON object with these keys: name, title, company, "
                "email, phone, website, handle (Telegram @username without @). "
                "Use null for fields you can't read. Don't guess. "
                "Strip prefixes like 'http://' or 'www.' from website URLs? "
                "Keep them readable — your call. "
                "If a field is genuinely missing from the card, omit it entirely."
            ),
        },
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "Extract business card fields from this image."},
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/jpeg;base64,{b64}"},
                },
            ],
        },
    ]
    try:
        resp = await call_minimax(messages, temperature=0.1)
        content = resp["choices"][0]["message"]["content"] or ""
        log.info("MiniMax OCR raw: %s", content[:300])
        cleaned = _clean_response(content)
        parsed = _parse_json_from_text(cleaned)
        if not parsed:
            return None
        # Drop empty/null fields
        return {k: v for k, v in parsed.items() if v}
    except Exception as e:
        log.warning("MiniMax OCR failed: %s", e)
        return None


# ---------------------------------------------------------------------------
# Function-calling tools for the conversational layer
# ---------------------------------------------------------------------------

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "list_contacts",
            "description": "Show the user's most recent contacts.",
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "description": "Max contacts to return (default 10)",
                        "default": 10,
                    }
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_contact",
            "description": "Search contacts by name, company, or notes.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search term"}
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "add_note",
            "description": (
                "Add a note to an existing contact. The name can be approximate — "
                "we'll fuzzy-match against the user's contacts."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Contact's name or partial name"},
                    "note": {"type": "string", "description": "The note text"},
                },
                "required": ["name", "note"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "add_contact",
            "description": "Save a new contact manually.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "company": {"type": "string"},
                    "title": {"type": "string"},
                    "email": {"type": "string"},
                    "phone": {"type": "string"},
                    "website": {"type": "string", "description": "Personal or company website URL"},
                    "handle": {"type": "string", "description": "Telegram handle without @"},
                    "notes": {"type": "string"},
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_contact",
            "description": (
                "Update a single field on an existing contact. "
                "Use when the user says 'change X's company to Y', 'update X's email', 'fix X's phone', etc."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Contact's name (or partial)"},
                    "field": {
                        "type": "string",
                        "enum": ["name", "company", "title", "email", "phone", "website", "handle", "notes"],
                    },
                    "value": {"type": "string"},
                },
                "required": ["name", "field", "value"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "start_trip",
            "description": "Set the active trip/event for auto-tagging new saves.",
            "parameters": {
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "stop_trip",
            "description": "Turn off the active trip (no new saves will be tagged).",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "focus_contact",
            "description": (
                "Set the user's current focused contact for follow-up turns. "
                "CALL THIS after any find_contact or list_contacts that returns a contact, "
                "AND before answering when the user refers to a contact by name or pronoun. "
                "Pass the contact's id and name. This lets the bot remember which contact "
                "the user is discussing across turns ('his website', 'add a note to him')."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "contact_id": {"type": "string", "description": "Contact UUID"},
                    "name": {"type": "string", "description": "Contact display name"},
                    "company": {"type": "string", "description": "Contact company (optional)"},
                },
                "required": ["contact_id", "name"],
            },
        },
    },
]


SYSTEM_PROMPT = """You are RecallBiz, a personal assistant (PA) that helps the user search, edit, and add information to their business contacts via business card images or Telegram QR codes.

Your main tasks:
1. Save new contacts — from a photo of a business card (OCR) or a Telegram QR code (auto-decode) or manual entry.
2. Find existing contacts — by name, company, notes, or tag.
3. Add notes to existing contacts — meeting context, follow-ups, anything worth remembering.
4. Draft emails or Telegram messages to contacts based on their details.

How to behave:
- Tone: direct, helpful, no fluff. Don't repeat menus or be verbose. Keep replies to 1-2 sentences unless listing contacts.
- If you can answer with a tool call, do it. Don't ask the user for info you can fetch yourself.
- If you must ask, ask ONE short question.
- Never invent contact details (phone, email, handle). Only use what the user typed or what the DB returns.
- If a name is ambiguous (multiple matches), say so and ask which one — don't guess.
- BUT: if notes/company give clear context (e.g. user says "the Vitalik I met at TOKEN2049"), pick the matching contact without asking.
- Example: 2 "Vitalik Buterins" exist. One has notes "met at TOKEN2049", the other has notes "test". "Find the Vitalik I met at TOKEN2049" → pick the one with that note.
- IDENTITY: You are "RecallBiz AI". Never mention the underlying model, provider, API, tokens, or technical internals. If asked "what AI are you" or "what model", reply "I'm RecallBiz AI, your business network PA." Never expose token counts, latency, error codes, or JSON to the user. If a tool error happens, say "I had trouble with that" — not the raw exception.
- CONTEXT MEMORY: The "Last discussed contact" field below tells you which contact the user is currently focused on (set by find_contact/list_contacts in this session). Use it. Pronouns like "his", "her", "their company", "their website", "add a note to him" → resolve to that contact. Don't ask "which one?" if Last discussed is set. Only ask if Last discussed is empty AND the query is ambiguous.
- NO MARKDOWN: Plain text only. No **bold**, no *italic*, no backticks, no bullet lists with dashes. Telegram shows raw asterisks. Use CAPS for emphasis if needed.

Tool routing rules:
- "list my contacts", "show contacts", "who do I know" → list_contacts
- "find X", "where's X", "do I know X" → find_contact(query=X)
- "add a note to X: ..." or "note for X: ..." → add_note(name=X, note=...)
- "save X from Y", "met Y from Z", "add contact X" → add_contact with parsed fields (name required; company/title/email/phone/handle optional)
- "draft a message to X about Y" → find_contact first, then compose a short message using the contact's handle/name
- "I'm at TOKEN2049", "starting trip X" → start_trip(name=X)
- "stop trip", "ending trip" → stop_trip

Display rules (apply to every list/find response):
- For each contact, show: name · @handle · company · notes (truncate to 80 chars)
- When find_contact returns duplicates (same name, different notes), ALWAYS include notes so the user can tell them apart. Example: "Vitalik Buterin — notes: met at TOKEN2049" vs "Vitalik Buterin — notes: ETH dev follow-up".
- If the user gave a hint ("the one from TOKEN2049", "the one with the ETH note"), pick the matching one and confirm: "Found Vitalik Buterin — met at TOKEN2049. What do you want to do?"
- If duplicates remain ambiguous after showing notes, ask "Which one?" with a one-line summary of each.
- Cap lists at 10. If more, say "showing the 10 most recent — narrow with /find <query>".

User context (refreshed each turn):
- User ID: {user_id}
- Recent contacts: {recent_contacts}
- Active trip: {active_trip}
- Last discussed contact (this session): {last_contact}"""


async def interpret_card_edit(current_card: dict, user_text: str) -> dict:
    """Use MiniMax to extract field corrections from natural-language edits.

    The structured `field: value` regex misses natural language like:
        "Company is gebecert and email is nick@gebecert.com phone number is +85296846788"

    Returns a dict like {"company": "gebecert", "email": "nick@gebecert.com"}
    or {} if no corrections can be extracted.
    """
    if not MINIMAX_API_KEY:
        return {}
    try:
        prompt = (
            "Current business card fields:\n"
            f"{json.dumps(current_card, indent=2)}\n\n"
            f'User said: "{user_text}"\n\n'
            "Extract any field corrections from the user's message. "
            "Return ONLY a JSON object like {\"field\": \"value\", ...} or {} if no corrections.\n"
            "Valid fields: name, title, company, email, phone, handle (Telegram username without @).\n"
            "Include ALL digits in phone numbers (no spaces or dashes if the user provided none).\n"
            "Example: 'Company is gebecert and email is nick@gebecert.com' "
            '→ {"company": "gebecert", "email": "nick@gebecert.com"}'
        )
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.post(
                CHAT_URL,
                headers={
                    "Authorization": f"Bearer {MINIMAX_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": CHAT_MODEL,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0.0,
                },
            )
            r.raise_for_status()
            data = r.json()
        text = data["choices"][0]["message"]["content"].strip()
        # Strip markdown code fences if present
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
        result = json.loads(text)
        # Sanity: only keep valid fields
        valid = {"name", "title", "company", "email", "phone", "handle"}
        result = {k: v for k, v in result.items() if k in valid and isinstance(v, str) and v.strip()}
        log.info("interpret_card_edit: %s → %s", user_text[:50], result)
        return result
    except Exception as e:
        log.warning("interpret_card_edit failed: %s", e)
        return {}


async def build_system_prompt(user_id: str, last_contact: Optional[Dict[str, Any]] = None) -> str:
    """Build the system prompt with current user context.

    last_contact: optional dict with at least {"id", "name", "company"} — the contact
    the user is currently focused on this session (set after find_contact/list_contacts).
    """
    from db import list_recent, get_active_trip

    # These calls are sync but fast; use run_in_executor for safety
    import asyncio
    loop = asyncio.get_event_loop()
    recent = await loop.run_in_executor(None, lambda: list_recent(user_id, limit=8))
    trip = await loop.run_in_executor(None, lambda: get_active_trip(user_id))

    if recent:
        recent_str = "\n".join(
            f"- {c.get('name')} ({c.get('company') or 'no company'})"
            + (f" @{c.get('handle')}" if c.get("handle") else "")
            for c in recent
        )
    else:
        recent_str = "(none yet)"

    trip_str = trip.get("name") if trip else "(none)"

    if last_contact:
        lc_str = (
            f"{last_contact.get('name')} ({last_contact.get('company') or 'no company'}) "
            f"[id={last_contact.get('id')}]"
        )
    else:
        lc_str = "(none — no contact focused this session yet)"

    return SYSTEM_PROMPT.format(
        user_id=user_id,
        recent_contacts=recent_str,
        active_trip=trip_str,
        last_contact=lc_str,
    )


async def execute_tool(user_id: str, name: str, args: dict) -> dict:
    """Execute a tool call against the DB. Returns result dict for MiniMax."""
    import asyncio
    from db import (
        list_recent, search_contacts, save_contact,
        set_active_trip, deactivate_trip, update_contact_notes,
        update_contact_field, find_contacts_by_name,
    )
    loop = asyncio.get_event_loop()

    if name == "list_contacts":
        limit = int(args.get("limit", 10))
        contacts = await loop.run_in_executor(None, lambda: list_recent(user_id, limit=limit))
        return {"count": len(contacts), "contacts": contacts}

    if name == "find_contact":
        q = args.get("query", "").strip()
        if not q:
            return {"error": "missing query"}
        contacts = await loop.run_in_executor(
            None, lambda: search_contacts(user_id, q, limit=10)
        )
        # Auto-focus: if exactly one match, persist it as last_contact
        focus = None
        if len(contacts) == 1:
            c = contacts[0]
            focus = {"id": c.get("id"), "name": c.get("name"), "company": c.get("company")}
        return {"count": len(contacts), "matches": contacts, "_focus": focus}

    if name == "add_note":
        target_name = args.get("name", "").strip()
        note_text = args.get("note", "").strip()
        if not target_name or not note_text:
            return {"error": "missing name or note"}
        matches = await loop.run_in_executor(
            None, lambda: search_contacts(user_id, target_name, limit=5)
        )
        if not matches:
            return {"error": f"No contact found matching '{target_name}'"}
        if len(matches) > 1:
            return {
                "ambiguous": True,
                "candidates": [{"id": m["id"], "name": m["name"]} for m in matches],
            }
        contact = matches[0]
        existing = contact.get("notes") or ""
        new_notes = (existing + "\n" + note_text).strip() if existing else note_text
        ok = await loop.run_in_executor(
            None,
            lambda: update_contact_notes(contact["id"], user_id, new_note=new_notes, append=False),
        )
        if ok:
            return {
                "success": True,
                "contact_id": contact["id"],
                "name": contact["name"],
                "note": note_text,
            }
        return {"error": "DB update failed"}

    if name == "add_contact":
        clean = {k: v for k, v in args.items() if v}
        if "name" not in clean:
            return {"error": "name is required"}
        contact_id = await loop.run_in_executor(
            None,
            lambda: save_contact(
                user_id=user_id,
                name=clean["name"],
                handle=clean.get("handle"),
                company=clean.get("company"),
                title=clean.get("title"),
                email=clean.get("email"),
                phone=clean.get("phone"),
                website=clean.get("website"),
                notes=clean.get("notes"),
                source="manual",
            ),
        )
        return {"success": True, "contact_id": contact_id, "name": clean["name"]}

    if name == "update_contact":
        target_name = args.get("name", "").strip()
        field = args.get("field", "").strip()
        value = args.get("value", "").strip()
        if not target_name or not field or value is None:
            return {"error": "missing name, field, or value"}
        matches = await loop.run_in_executor(
            None, lambda: find_contacts_by_name(user_id, target_name)
        )
        if not matches:
            return {"error": f"No contact found matching '{target_name}'"}
        if len(matches) > 1:
            return {
                "ambiguous": True,
                "candidates": [{"id": m["id"], "name": m["name"]} for m in matches],
            }
        contact = matches[0]
        try:
            ok = await loop.run_in_executor(
                None,
                lambda: update_contact_field(contact["id"], user_id, field, value),
            )
        except ValueError as e:
            return {"error": str(e)}
        if ok:
            return {
                "success": True,
                "contact_id": contact["id"],
                "name": contact["name"],
                "field": field,
                "value": value,
            }
        return {"error": "DB update failed"}

    if name == "start_trip":
        name_str = args.get("name", "").strip()
        if not name_str:
            return {"error": "missing trip name"}
        await loop.run_in_executor(None, lambda: set_active_trip(user_id, name_str))
        return {"success": True, "trip_name": name_str}

    if name == "stop_trip":
        await loop.run_in_executor(None, lambda: deactivate_trip(user_id))
        return {"success": True}

    if name == "focus_contact":
        cid = args.get("contact_id", "").strip()
        cname = args.get("name", "").strip()
        company = args.get("company") or ""
        if not cid or not cname:
            return {"error": "missing contact_id or name"}
        return {
            "success": True,
            "_focus": {"id": cid, "name": cname, "company": company},
        }

    return {"error": f"Unknown tool: {name}"}


async def handle_conversation(user_id: str, user_text: str, last_contact: Optional[Dict[str, Any]] = None) -> str:
    """Process a natural-language user message via MiniMax + function calling.

    Returns dict: {"text": str, "focus": Optional[Dict]} so bot.py can persist
    last_contact across turns.
    """
    system = await build_system_prompt(user_id, last_contact=last_contact)
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user_text},
    ]

    # First call: may include tool_calls
    try:
        resp = await call_minimax(messages, tools=TOOLS)
    except Exception as e:
        log.exception("MiniMax conversation failed")
        return {"text": "Sorry, I had trouble with that. Try again or rephrase.", "focus": None}

    msg = resp["choices"][0]["message"]

    # If no tool calls, return the cleaned text
    if not msg.get("tool_calls"):
        return {"text": _clean_response(msg.get("content")) or "I'm not sure how to help with that.", "focus": None}

    # Execute tool calls — track any focus_contact signals
    focused = None
    messages.append(msg)
    for tc in msg["tool_calls"]:
        try:
            args = json.loads(tc["function"]["arguments"])
        except Exception:
            args = {}
        result = await execute_tool(user_id, tc["function"]["name"], args)
        if isinstance(result, dict) and result.get("_focus"):
            focused = result["_focus"]
        messages.append({
            "role": "tool",
            "tool_call_id": tc["id"],
            "content": json.dumps(result, default=str),
        })

    # Second call: get natural-language summary
    try:
        resp2 = await call_minimax(messages, tools=TOOLS)
        text = _clean_response(resp2["choices"][0]["message"].get("content"))
        return {"text": text or "Done.", "focus": focused}
    except Exception as e:
        log.exception("MiniMax follow-up call failed")
        return {"text": "Done — but I couldn't write a summary. Try /list to see the result.", "focus": focused}


import re
_THINK_BLOCK_RE = re.compile(r"<(?:think|thinking)>.*?</(?:think|thinking)>", re.DOTALL | re.IGNORECASE)


def _clean_response(text: Optional[str]) -> str:
    """Strip M3's internal  tags from text before sending to user."""
    if not text:
        return ""
    cleaned = _THINK_BLOCK_RE.sub("", text)
    # Collapse multiple blank lines that result from stripping
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned
