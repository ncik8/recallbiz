"""TRCE (formerly RecallBiz) Telegram bot — main entry point."""
import os
import re
import asyncio
import logging
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

from db import (
    init_db, log_usage, save_contact, search_contacts, list_recent,
    set_active_trip, get_active_trip, deactivate_trip, count_trip_contacts,
    get_or_create_user, get_filtered_contacts, update_contact_notes,
    update_contact_field, find_contacts_by_name,
)
from ocr import try_decode_qr, parse_telegram_qr
from ai import interpret_card_edit
from ai import extract_card_from_image, handle_conversation


async def _resolve_user_id(update, context) -> str:
    """Resolve telegram_user_id -> internal users.id UUID. Cached in context.user_data."""
    cached = context.user_data.get("user_id")
    if cached:
        return cached
    tg_user = update.effective_user
    user_id = await asyncio.get_event_loop().run_in_executor(
        None,
        lambda: get_or_create_user(
            telegram_user_id=tg_user.id,
            username=tg_user.username,
            display_name=tg_user.full_name,
        ),
    )
    context.user_data["user_id"] = user_id
    return user_id


async def _is_signed_up(user_id: str) -> bool:
    """True if the user is cleared to save (signed up via email OR marked as tester).

    Used as a soft gate: blocking all save actions until /signup completes.
    Fails OPEN on any error so a DB hiccup never breaks a real command.
    """
    try:
        from db import get_user
        user = await asyncio.get_event_loop().run_in_executor(None, lambda: get_user(user_id))
        if not user:
            return False
        return bool(user.get("is_tester")) or bool(user.get("email_verified"))
    except Exception as e:
        log.warning("_is_signed_up check failed: %s", e)
        return True  # fail open


async def _require_signup(update, context, action: str = "use that") -> bool:
    """Sign-up gate for handlers. Sends a friendly prompt + returns True
    if the user is NOT signed up (caller should `return` immediately).
    Returns False if signed up (caller should proceed).

    Use this in /list, /find, /trip, /send, /save, photo_handler,
    contact_handler, echo_text. The /start, /help, /signup, and
    /stats commands stay open so onboarding isn't blocked.

    `action` is a short verb phrase used in the prompt, e.g.
        _require_signup(update, context, action="search contacts")
    → "Quick signup to search contacts..."
    """
    user_id = await _resolve_user_id(update, context)
    if await _is_signed_up(user_id):
        return False
    log_usage(user_id, "signup_gate_blocked", details=action)
    await update.message.reply_text(
        f"Quick signup to {action} — keeps your data safe.\n\n"
        f"/signup you@gmail.com\n"
        f"(Free plan = 10 contacts, takes ~30 sec)"
    )
    return True


load_dotenv()
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=os.environ.get("LOG_LEVEL", "INFO"),
)
log = logging.getLogger(__name__)


APP_URL_PUBLIC = os.environ.get("APP_URL_PUBLIC", "https://trce.io")


WELCOME = """👋 Welcome to TRCE — Trace AI.

Your business card scanner + personal CRM, right inside Telegram. Never forget a contact again.

📌 PIN THIS CHAT
Long-press in your chat list → "Pin". You'll want TRCE at the top during events.

📋 MENU
  /signup — required before any save (free = 10 contacts)
  /save  — add a contact manually
  /list  — your recent contacts
  /find  — search by name, company, notes
  /trip  — start/stop a trip (auto-tag who you meet)
  /help  — all commands + how-tips

Type / anytime to see this menu.

HOW IT WORKS
1. Type /signup you@gmail.com — we send a magic link to confirm
2. Click it, you're in (free plan = 10 contacts)
3. Forward a Telegram QR, send a photo of a card, or share a contact"""

HELP = """Commands:
  /start — Welcome + onboarding
  /save — Manually save a contact
  /list — Show your recent contacts
  /find <query> — Search contacts (name, company, notes, tag)
  /trip set <name> — Start a trip (auto-tag new saves)
  /trip on/off — Toggle trip mode without changing the trip
  /trip — Show current trip + count
  /send <filter> <message> — Generate t.me links to message a filtered group
  /help — This message

Tip: Forward any Telegram QR image to me and I'll save the contact automatically.
You can also share a Telegram contact card and I'll grab the name + phone.

Filters for /send:
  tag:investor    — all contacts tagged X
  event:Token2049  — all contacts from event X (or current trip)
  trip:Token2049   — same as event:
  all              — every contact with a Telegram handle"""


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = await _resolve_user_id(update, context)
    log_usage(user_id, "start")

    # Magic link deep-link: /start verify_<token>
    args = (context.args or [])
    if args and args[0].startswith("verify_"):
        token = args[0][len("verify_"):]
        from auth import complete_signup
        result = await complete_signup(token)
        if result.get("success"):
            reply = (
                f"Email confirmed: {result['email']}\n"
                f"You're signed up. Free plan includes 10 contacts. "
                f"/help for everything the bot can do."
            )
            # Resume any pending save that was gated behind signup.
            pending = context.user_data.pop("pending_signup_resume", None)
            if pending:
                ptype = pending.get("type")
                if ptype == "contact_share":
                    # Contact share is atomic — re-call save_contact with stashed args.
                    try:
                        contact_id = await asyncio.get_event_loop().run_in_executor(
                            None,
                            lambda: save_contact(
                                user_id=user_id,
                                name=pending["name"],
                                phone=pending.get("phone"),
                                telegram_user_id=pending.get("telegram_user_id"),
                                source="telegram_share",
                            ),
                        )
                        log_usage(user_id, "contact_saved_resumed", f"contact_id={contact_id}")
                        extra = "\n".join(
                            [f"Phone: {pending['phone']}"] +
                            (["(Telegram user)"] if pending.get("telegram_user_id") else [])
                        )
                        reply += f"\n\nResumed your last share: saved {pending['name']}.\n{extra}"
                    except Exception as e:
                        log.exception("resume contact_share failed")
                        reply += "\n\nCouldn't auto-resume your last share — please re-send it."
                elif ptype == "photo":
                    reply += (
                        "\n\nResuming your last photo — give me a sec to read the card..."
                    )
                    # Re-fetch the photo file by stashed file_id and OCR it.
                    try:
                        from ocr import extract_card_from_image, try_decode_qr
                        tg_file = await context.bot.get_file(pending["file_id"])
                        photo_bytes = bytes(await tg_file.download_as_bytearray())
                        # Try QR first
                        qr = try_decode_qr(photo_bytes)
                        if qr:
                            from db import parse_telegram_qr  # ensure import
                            reply += f"\n(Detected Telegram QR: {qr[:80]})"
                        else:
                            extracted = await extract_card_from_image(photo_bytes)
                            if extracted and (extracted.get("name") or extracted.get("email") or extracted.get("phone")):
                                contact_id = await asyncio.get_event_loop().run_in_executor(
                                    None,
                                    lambda: save_contact(
                                        user_id=user_id,
                                        name=extracted.get("name", "Unknown"),
                                        handle=extracted.get("handle"),
                                        company=extracted.get("company"),
                                        title=extracted.get("title"),
                                        email=extracted.get("email"),
                                        phone=extracted.get("phone"),
                                        website=extracted.get("website"),
                                        source="paper_ocr",
                                    ),
                                )
                                reply += f"\nSaved from card: {extracted.get('name')} ({extracted.get('company') or 'no company'})"
                            else:
                                reply += "\nCouldn't read the card. Try /save to add manually."
                    except Exception as e:
                        log.exception("resume photo failed")
                        reply += "\nCouldn't auto-resume your last photo — please re-send it."
                elif ptype == "manual_save":
                    # Re-enter the /save wizard with stashed partial data.
                    for k, v in pending.get("partial", {}).items():
                        context.user_data[k] = v
                    context.user_data["save_step"] = pending.get("next_step", "handle")
                    reply += "\n\nResuming your /save. What's their Telegram handle? (or /skip)"
            await update.message.reply_text(reply)
        else:
            await update.message.reply_text(
                f"Couldn't verify: {result.get('error', 'unknown error')}\n"
                f"Try /signup <your-email> again."
            )
        return

    await update.message.reply_text(WELCOME)


async def signup_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Begin email signup: generate token + send magic link via Resend."""
    from auth import start_signup
    user_id = await _resolve_user_id(update, context)
    log_usage(user_id, "signup_start")

    if not context.args:
        await update.message.reply_text(
            "Send your email like this:\n\n/signup you@gmail.com"
        )
        return

    email = " ".join(context.args).strip()
    result = await start_signup(user_id, email)
    if result.get("success"):
        email = result["email"]
        if result.get("dev_mode"):
            # No Resend key — log the link so Nick can click it manually
            await update.message.reply_text(
                f"DEV MODE: Resend isn't configured, so I logged your magic link "
                f"in the bot's terminal output. Open it to confirm:\n\n{result['link']}"
            )
        else:
            await update.message.reply_text(
                f"Sent a confirmation link to {email}. "
                f"Click it within 15 minutes to activate your account."
            )
    else:
        await update.message.reply_text(result.get("error", "Signup failed. Try again."))


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP)


async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Founder-only stats dashboard. Gated by Telegram user_id.

    Access:    Only the founder (default 6045136979, override via FOUNDER_TG_ID env)
    Shows:     users, contacts, signups today/7d, events, active trips, pending links
    """
    founder_id = int(os.environ.get("FOUNDER_TG_ID", "6045136979"))
    caller_id = update.effective_user.id if update.effective_user else None
    if caller_id != founder_id:
        # Don't reveal the command exists to non-founders.
        await update.message.reply_text("Unknown command. /help for the list.")
        log.warning("/stats attempted by non-founder user_id=%s", caller_id)
        return

    from db import get_founder_stats
    stats = get_founder_stats()

    def fmt(n):
        return "?" if n < 0 else f"{n:,}"

    lines = [
        "TRCE stats",
        "",
        f"Users:        {fmt(stats['users_total'])} total, "
        f"{fmt(stats['users_signed_up'])} signed up",
        f"Signups:      {fmt(stats['signups_today'])} today, "
        f"{fmt(stats['signups_last_7d'])} last 7d",
        f"Contacts:     {fmt(stats['contacts_total'])} total, "
        f"{fmt(stats['contacts_today'])} today",
        f"Events:       {fmt(stats['events_today'])} today, "
        f"{fmt(stats['events_last_7d'])} last 7d",
        f"Active trips: {fmt(stats['active_trips'])}",
        f"Pending links: {fmt(stats['magic_links_pending'])}",
    ]
    await update.message.reply_text("\n".join(lines))


async def save_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Manual save flow — interactive prompts."""
    user_id = await _resolve_user_id(update, context)
    log_usage(user_id, "save_start")

    # Signup gate — block /save until email is verified (testers bypass).
    if not await _is_signed_up(user_id):
        log_usage(user_id, "signup_gate_blocked", details="manual_save")
        await update.message.reply_text(
            "Quick signup to keep your contacts safe.\n\n"
            "/signup you@gmail.com\n"
            "(Free plan = 10 contacts, ~30 sec)\n\n"
            "Then /save again to add this contact."
        )
        return

    context.user_data["save_step"] = "name"
    await update.message.reply_text(
        "Let's save a contact.\n\nWhat's their name? (or /cancel)"
    )


async def _handle_save_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Process the current save step."""
    step = context.user_data.get("save_step")
    text = update.message.text.strip()

    if step == "name":
        context.user_data["new_contact_name"] = text
        context.user_data["save_step"] = "handle"
        await update.message.reply_text(
            f"Got it: {text}\n\nTelegram handle? (e.g. @username, or /skip)"
        )
    elif step == "handle":
        h = text.lstrip("@") if text != "/skip" else None
        context.user_data["new_contact_handle"] = h
        context.user_data["save_step"] = "company"
        await update.message.reply_text("Company? (or /skip)")
    elif step == "company":
        context.user_data["new_contact_company"] = text if text != "/skip" else None
        context.user_data["save_step"] = "title"
        await update.message.reply_text("Title? (or /skip)")
    elif step == "title":
        context.user_data["new_contact_title"] = text if text != "/skip" else None
        context.user_data["save_step"] = "notes"
        await update.message.reply_text("Notes? (or /skip)")
    elif step == "notes":
        notes = None if text == "/skip" else text
        # Save!
        user_id = context.user_data.get("user_id")
        contact_id = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: save_contact(
                user_id=user_id,
                name=context.user_data["new_contact_name"],
                handle=context.user_data.get("new_contact_handle"),
                company=context.user_data.get("new_contact_company"),
                title=context.user_data.get("new_contact_title"),
                notes=notes,
                source="manual",
            ),
        )
        log_usage(user_id, "save_complete", f"contact_id={contact_id}")
        # Clear state
        for k in ["save_step", "new_contact_name", "new_contact_handle",
                  "new_contact_company", "new_contact_title"]:
            context.user_data.pop(k, None)

        trip = await asyncio.get_event_loop().run_in_executor(
            None, lambda: get_active_trip(user_id)
        )
        trip_note = f"\nTagged with: {trip['name']}" if trip else ""
        await update.message.reply_text(
            f"✓ Saved.{trip_note}\n\nUse /save for another, /list to see all, /find to search."
        )


async def cancel_save(update: Update, context: ContextTypes.DEFAULT_TYPE):
    for k in ["save_step", "new_contact_name", "new_contact_handle",
              "new_contact_company", "new_contact_title"]:
        context.user_data.pop(k, None)
    await update.message.reply_text("Save cancelled.")


async def list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await _require_signup(update, context, action="see your contacts"):
        return
    user_id = await _resolve_user_id(update, context)
    log_usage(user_id, "list")
    rows = await asyncio.get_event_loop().run_in_executor(
        None, lambda: list_recent(user_id, limit=10)
    )
    if not rows:
        await update.message.reply_text(
            "No contacts yet. Try /save or forward a Telegram QR image."
        )
        return
    lines = ["Your last 10 contacts:\n"]
    for i, c in enumerate(rows, 1):
        h = f" @{c['handle']}" if c.get("handle") else ""
        co = f" — {c['company']}" if c.get("company") else ""
        ti = f", {c['title']}" if c.get("title") else ""
        when = c["saved_at"][:16] if c.get("saved_at") else ""
        lines.append(f"{i}. {c['name']}{h}{co}{ti} · {when}")
    await update.message.reply_text("\n".join(lines))


async def find_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await _require_signup(update, context, action="search contacts"):
        return
    user_id = await _resolve_user_id(update, context)
    query = " ".join(context.args) if context.args else ""
    if not query:
        await update.message.reply_text("Usage: /find <query>\n\nExample: /find polychain")
        return
    log_usage(user_id, "find", f"query={query}")
    rows = await asyncio.get_event_loop().run_in_executor(
        None, lambda: search_contacts(user_id, query, limit=10)
    )
    if not rows:
        await update.message.reply_text(f"No contacts matching '{query}'.")
        return
    lines = [f"{len(rows)} contact(s) matching '{query}':\n"]
    for i, c in enumerate(rows, 1):
        h = f" @{c['handle']}" if c.get("handle") else ""
        co = f" — {c['company']}" if c.get("company") else ""
        ti = f", {c['title']}" if c.get("title") else ""
        lines.append(f"{i}. {c['name']}{h}{co}{ti}")
    await update.message.reply_text("\n".join(lines))


async def trip_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await _require_signup(update, context, action="use trip mode"):
        return
    user_id = await _resolve_user_id(update, context)
    args = context.args
    if not args:
        trip = await asyncio.get_event_loop().run_in_executor(
            None, lambda: get_active_trip(user_id)
        )
        if trip:
            count = await asyncio.get_event_loop().run_in_executor(
                None, lambda: count_trip_contacts(trip["id"])
            )
            await update.message.reply_text(
                f"Current trip: {trip['name']}\n"
                f"Saved this trip: {count} contacts\n"
                f"Started: {trip.get('start_date') or '—'}"
            )
        else:
            await update.message.reply_text(
                "No active trip.\n\nStart one with: /trip set <event name>"
            )
        return

    if args[0] == "set" and len(args) > 1:
        event_name = " ".join(args[1:])
        await asyncio.get_event_loop().run_in_executor(
            None, lambda: set_active_trip(user_id, event_name)
        )
        log_usage(user_id, "trip_set", f"name={event_name}")
        await update.message.reply_text(
            f"✓ Trip set: {event_name}\n"
            f"All new saves auto-tagged until /trip off."
        )
    elif args[0] == "off":
        await asyncio.get_event_loop().run_in_executor(
            None, lambda: deactivate_trip(user_id)
        )
        await update.message.reply_text("✓ Trip mode off. New saves not tagged.")
    elif args[0] == "on":
        await update.message.reply_text(
            "Use /trip set <name> to start a new trip."
        )
    else:
        await update.message.reply_text(
            "Usage:\n"
            "  /trip set <event name> — start a trip\n"
            "  /trip off — stop tagging\n"
            "  /trip — show current trip"
        )


async def send_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Generate t.me deep links to message a filtered group of contacts.

    The bot drafts, the user sends. Each link opens the chat with the
    message pre-filled — user edits if needed, taps send.

    Usage:
      /send tag:investor Hey, 15min next week?
      /send event:Token2049 Following up from the panel
      /send all Quick hello
    """
    from urllib.parse import quote

    if await _require_signup(update, context, action="message your contacts"):
        return
    user_id = await _resolve_user_id(update, context)

    args = context.args
    if not args or len(args) < 2:
        await update.message.reply_text(
            "Usage: /send <filter> <message>\n\n"
            "Filters:\n"
            "  tag:investor       all contacts tagged 'investor'\n"
            "  event:Token2049    all contacts from event 'Token2049'\n"
            "  trip:Token2049     same as event:\n"
            "  all                every contact with a handle\n\n"
            "Example:\n"
            "  /send tag:investor Hey, 15min next week to compare notes?"
        )
        return

    filter_str = args[0]
    message = " ".join(args[1:])
    log_usage(user_id, "send", f"filter={filter_str} msg_len={len(message)}")

    if ":" not in filter_str:
        await update.message.reply_text(
            "Filter must be tag:X, event:X, trip:X, or all"
        )
        return

    filter_type, filter_value = filter_str.split(":", 1)
    filter_value = filter_value.strip()

    rows = await asyncio.get_event_loop().run_in_executor(
        None, lambda: get_filtered_contacts(user_id, filter_type, filter_value)
    )

    if not rows:
        await update.message.reply_text(
            f"No contacts with handles matching {filter_str}.\n\n"
            f"Use /find to verify what's saved."
        )
        return

    encoded_msg = quote(message)
    inline_links = []
    no_handle = 0
    for r in rows:
        if r["handle"]:
            url = f"https://t.me/{r['handle']}?text={encoded_msg}"
            inline_links.append((r["name"], r["handle"], url))
        else:
            no_handle += 1

    if not inline_links:
        await update.message.reply_text(
            f"{len(rows)} contacts matched {filter_str}, but none have a Telegram handle.\n\n"
            f"Use /save to add handles."
        )
        return

    # Inline buttons (Telegram limit ~100 per message; cap at 30 for readability)
    shown = inline_links[:30]
    more = len(inline_links) - len(shown)

    keyboard = []
    for name, handle, _ in shown:
        keyboard.append(
            [InlineKeyboardButton(f"→ {name} (@{handle})", url=f"https://t.me/{handle}?text={encoded_msg}")]
        )

    summary = (
        f"📤 Draft for {len(inline_links)} contact(s) matching {filter_str}\n\n"
        f"Message:\n\"{message}\"\n\n"
        f"Tap each button to open chat with message pre-filled.\n"
        f"Edit if needed, then send.\n"
    )
    if more > 0:
        summary += f"\n+ {more} more (not shown — use a more specific filter to narrow down)"
    if no_handle > 0:
        summary += f"\n\n⚠ {no_handle} matched contact(s) skipped — no Telegram handle saved."

    await update.message.reply_text(
        summary,
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def echo_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Route non-command text: pending states first, else AI conversation."""
    if context.user_data.get("pending_note_for"):
        await _save_note(update, context)
        return
    if context.user_data.get("pending_card"):
        await _confirm_ocr_save(update, context)
        return
    if context.user_data.get("save_step"):
        await _handle_save_message(update, context)
        return

    # No pending state — route to MiniMax conversational layer.
    # Gate: only signed-up users can chat with the AI.
    if await _require_signup(update, context, action="chat with the AI"):
        return
    user_id = context.user_data.get("user_id") or await _resolve_user_id(update, context)
    user_text = update.message.text.strip()
    if not user_text:
        return
    processing = await update.message.reply_text("🤔 Thinking...")
    try:
        ud = context.user_data or {}
        last_contact = ud.get("last_contact")
        result = await handle_conversation(user_id, user_text, last_contact=last_contact)
        # handle_conversation returns {"text": str, "focus": Optional[Dict]}
        if isinstance(result, dict):
            reply = result.get("text") or "Done."
            focused = result.get("focus")
            if focused:
                ud["last_contact"] = focused
                log.info("focused contact set: %s (%s)", focused.get("name"), focused.get("id"))
        else:
            # Backward compat if handle_conversation ever returns a bare string
            reply = result or "Done."
        await processing.edit_text(reply)
    except Exception as e:
        log.exception("handle_conversation failed")
        await processing.edit_text(
            "Sorry, I had trouble with that. Try again or rephrase."
        )


async def contact_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Native Telegram contact share — save name + phone from message.contact.

    Triggered when a user taps 'Share Contact' (or forwards a contact card).
    The shared contact has: first_name, last_name (optional), phone_number,
    and user_id (only if they're a Telegram user with the bot in scope).
    """
    user_id = await _resolve_user_id(update, context)
    log_usage(user_id, "contact_shared")

    contact = update.message.contact
    if not contact:
        return

    # Tier / limit check BEFORE we touch the DB.
    from db import check_contact_limit
    loop = asyncio.get_event_loop()
    limit = await loop.run_in_executor(None, lambda: check_contact_limit(user_id))
    if not limit.get("allowed"):
        log_usage(user_id, "limit_blocked", details=limit.get("reason", ""))
        await update.message.reply_text(
            f"You've used all {limit['limit']} free contacts.\n"
            f"Sign up at {APP_URL_PUBLIC} for unlimited saves."
        )
        return

    # Compose display name — fall back to phone if name missing
    first = (contact.first_name or "").strip()
    last = (contact.last_name or "").strip()
    name = f"{first} {last}".strip() or f"Contact {contact.phone_number}"

    # Strip spaces from phone for consistent storage
    phone = (contact.phone_number or "").replace(" ", "").replace("-", "") or None
    tg_uid = contact.user_id  # int or None — only set if they're a Telegram user

    # Signup gate — block first save until email is verified (testers bypass).
    if not await _is_signed_up(user_id):
        log_usage(user_id, "signup_gate_blocked", details="contact_share")
        # Stash for auto-resume after /signup completes.
        context.user_data["pending_signup_resume"] = {
            "type": "contact_share",
            "name": name,
            "phone": phone,
            "telegram_user_id": tg_uid,
        }
        await update.message.reply_text(
            "Quick signup to keep your contacts safe.\n\n"
            "/signup you@gmail.com\n"
            "(Free plan = 10 contacts, ~30 sec)\n\n"
            f"I'll auto-save {name} after you confirm."
        )
        return

    try:
        contact_id = await loop.run_in_executor(
            None,
            lambda: save_contact(
                user_id=user_id,
                name=name,
                phone=phone,
                telegram_user_id=tg_uid,
                source="telegram_share",
            ),
        )
        log_usage(user_id, "contact_saved", f"contact_id={contact_id}")
        remaining = (limit.get("limit", 10) - limit.get("current", 0) - 1) if limit.get("plan") == "free" else None
        lines = [f"Saved: {name}"]
        if phone:
            lines.append(f"Phone: {phone}")
        if tg_uid:
            lines.append("(Telegram user)")
        if remaining is not None and remaining <= 3:
            lines.append(f"\n{remaining} free contact(s) left. /signup for unlimited.")
        elif remaining is not None:
            lines.append(f"\nReply with anything to add (company, notes, etc.) or just ignore.")
        else:
            lines.append("\nReply with anything to add (company, notes, etc.) or just ignore.")
        await update.message.reply_text("\n".join(lines))
    except Exception as e:
        log.exception("contact_handler save_contact failed")
        await update.message.reply_text(
            "Couldn't save that contact. Try /save to add manually."
        )


async def photo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Image handler — try QR first (instant), then paper card OCR (slower)."""
    user_id = await _resolve_user_id(update, context)
    log_usage(user_id, "photo_received")

    photos = update.message.photo
    if not photos:
        await update.message.reply_text(
            "Send me a photo of a Telegram QR or a paper business card."
        )
        return

    # Tier / limit check BEFORE we do any expensive work.
    from db import check_contact_limit
    loop = asyncio.get_event_loop()
    limit = await loop.run_in_executor(None, lambda: check_contact_limit(user_id))
    if not limit.get("allowed"):
        log_usage(user_id, "limit_blocked", details=limit.get("reason", ""))
        await update.message.reply_text(
            f"You've used all {limit['limit']} free contacts.\n"
            f"Sign up at {APP_URL_PUBLIC} for unlimited saves."
        )
        return

    # Signup gate — block first save until email is verified (testers bypass).
    if not await _is_signed_up(user_id):
        log_usage(user_id, "signup_gate_blocked", details="photo")
        # Stash the largest photo's file_id for resume after signup.
        largest = photos[-1]
        context.user_data["pending_signup_resume"] = {
            "type": "photo",
            "file_id": largest.file_id,
        }
        await update.message.reply_text(
            "Quick signup to keep your contacts safe.\n\n"
            "/signup you@gmail.com\n"
            "(Free plan = 10 contacts, ~30 sec)\n\n"
            "I'll auto-OCR this card and save the contact after you confirm."
        )
        return

    processing_msg = await update.message.reply_text("🔍 Reading...")

    try:
        # Download highest-resolution version
        photo_file = await photos[-1].get_file()
        photo_bytes = bytes(await photo_file.download_as_bytearray())

        # Path 1: QR code (instant)
        qr_data = try_decode_qr(photo_bytes)
        if qr_data:
            await _save_from_qr(update, context, qr_data, user_id, processing_msg)
            return

        # Path 2: Paper card OCR via MiniMax vision (3-8s)
        await processing_msg.edit_text("🔍 No QR found. Reading the card with AI...")
        extracted = await extract_card_from_image(photo_bytes)
        if extracted and (extracted.get("name") or extracted.get("email") or extracted.get("phone")):
            # If critical fields missing (name + company), ask the user to fill the gaps.
            missing_critical = []
            if not extracted.get("name"):
                missing_critical.append("name")
            if not extracted.get("company"):
                missing_critical.append("company")
            if missing_critical:
                context.user_data["pending_card"] = extracted
                lines = ["📇 Got partial info from the card:\n"]
                for field, label in [
                    ("name", "Name"), ("title", "Title"), ("company", "Company"),
                    ("email", "Email"), ("phone", "Phone"), ("website", "Website"),
                    ("handle", "Telegram"),
                ]:
                    if extracted.get(field):
                        lines.append(f"{label}: {extracted[field]}")
                lines.append(f"\nMissing: {', '.join(missing_critical)}")
                lines.append("Reply with what you can fill in, e.g. 'name: John Smith'")
                lines.append("Or /cancel to discard.")
                await processing_msg.edit_text("\n".join(lines))
                return
            await _show_ocr_preview(update, context, extracted, user_id)
        else:
            await processing_msg.edit_text(
                "Couldn't read this image. Try:\n"
                "• A clearer photo with better lighting\n"
                "• Flat-lay the card (no curves)\n"
                "• Or /save to add manually"
            )
    except Exception as e:
        log.exception("photo_handler failed")
        await processing_msg.edit_text(f"Error: {e}")


async def _save_from_qr(update, context, qr_url, user_id, processing_msg):
    """Save contact from Telegram QR code. Auto-saves, no confirmation."""
    handle = parse_telegram_qr(qr_url)
    if not handle:
        await processing_msg.edit_text(
            f"📷 Got a QR but it's not a Telegram QR:\n{qr_url[:200]}"
        )
        return

    try:
        chat = await context.bot.get_chat(f"@{handle}")
    except Exception as e:
        log.warning("getChat(@%s) failed: %s", handle, e)
        await processing_msg.edit_text(
            f"Couldn't find @{handle} on Telegram. They may have a private account."
        )
        return

    display_name = chat.full_name or chat.title or handle
    contact_id = await asyncio.get_event_loop().run_in_executor(
        None,
        lambda: save_contact(
            user_id=user_id,
            name=display_name,
            handle=handle,
            telegram_user_id=chat.id,
            source="telegram_qr",
        ),
    )
    log_usage(user_id, "save_from_qr", f"contact_id={contact_id} handle={handle}")
    await processing_msg.edit_text(
        f"✓ Saved from QR:\n\n"
        f"{display_name}\n"
        f"@{handle}"
    )


async def _show_ocr_preview(update, context, extracted, user_id):
    """Show OCR preview and ask for confirmation."""
    context.user_data["pending_card"] = extracted

    lines = ["📇 I read this card:\n"]
    for field, label in [
        ("name", "Name"),
        ("title", "Title"),
        ("company", "Company"),
        ("email", "Email"),
        ("phone", "Phone"),
        ("website", "Website"),
        ("handle", "Telegram"),
    ]:
        if extracted.get(field):
            lines.append(f"{label}: {extracted[field]}")
    lines.append("\nReply YES to save.")
    lines.append("Or fix a field, e.g.  name: John Smith")
    lines.append("Or NO to discard.")

    await update.message.reply_text("\n".join(lines))


async def _confirm_ocr_save(update, context):
    """Handle YES / edit / NO reply to OCR confirmation."""
    extracted = context.user_data.get("pending_card")
    if not extracted:
        return False

    text = update.message.text.strip()

    # YES → save, then offer to add a note
    if text.lower() in ("yes", "y", "save"):
        user_id = context.user_data["user_id"]
        contact_id = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: save_contact(
                user_id=user_id,
                name=extracted.get("name") or "Unknown",
                handle=extracted.get("handle"),
                company=extracted.get("company"),
                title=extracted.get("title"),
                email=extracted.get("email"),
                phone=extracted.get("phone"),
                website=extracted.get("website"),
                source="paper_ocr",
            ),
        )
        log_usage(user_id, "save_from_ocr", f"contact_id={contact_id}")
        context.user_data.pop("pending_card", None)
        # Offer to add a note — set state for next text message
        context.user_data["pending_note_for"] = {
            "contact_id": contact_id,
            "name": extracted.get("name") or "this contact",
        }
        await update.message.reply_text(
            f"✓ Saved {extracted.get('name') or 'contact'}.\n\n"
            f"Want to add a note? Just type it, or say 'skip'."
        )
        return True

    # NO → discard
    if text.lower() in ("no", "n", "cancel", "discard"):
        context.user_data.pop("pending_card", None)
        await update.message.reply_text(
            "Discarded. Try a different photo or /save to add manually."
        )
        return True

    # Edit: "field: value" pattern (structured)
    match = re.match(r"(\w+)\s*:\s*(.+)", text, re.IGNORECASE)
    if match:
        field, value = match.groups()
        field = field.strip().lower()
        if field in ("name", "title", "company", "email", "phone", "website", "handle", "telegram"):
            extracted[field if field != "telegram" else "handle"] = value.strip()
            context.user_data["pending_card"] = extracted
            await update.message.reply_text(
                f"Updated {field}.\n\nReply YES to save, or keep editing."
            )
            return True

    # Natural-language edit: ask MiniMax to extract corrections
    if not text.lower().startswith(("yes", "y", "save", "no", "n", "cancel", "discard")):
        updates = await interpret_card_edit(extracted, text)
        if updates:
            extracted.update(updates)
            context.user_data["pending_card"] = extracted
            await update.message.reply_text(
                f"Got it — updated {', '.join(updates.keys())}.\n\n"
                f"Reply YES to save, or keep editing."
            )
            return True

    # Default
    await update.message.reply_text(
        "Reply YES to save, NO to discard.\n"
        "Or fix a field — either style works:\n"
        "  company: Gebecert\n"
        "  Company is gebecert, email is nick@gebecert.com"
    )
    return True


async def _save_note(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Save a note to a just-created contact."""
    pending = context.user_data.get("pending_note_for")
    if not pending:
        return
    contact_id = pending["contact_id"]
    contact_name = pending["name"]
    text = update.message.text.strip()

    # Skip
    if text.lower() in ("skip", "no", "n", "cancel", "done"):
        context.user_data.pop("pending_note_for", None)
        await update.message.reply_text(
            "OK, no note added.\n\nUse /save for another, /list to see all."
        )
        return

    # Append to existing notes
    from db import get_client as _gc
    def _append():
        client = _gc()
        existing = client.table("contacts").select("notes").eq("id", contact_id).maybe_single().execute()
        prev = (existing.data or {}).get("notes") or ""
        new_notes = (prev + "\n" + text).strip() if prev else text
        return client.table("contacts").update({"notes": new_notes}).eq("id", contact_id).execute()

    result = await asyncio.get_event_loop().run_in_executor(None, _append)
    context.user_data.pop("pending_note_for", None)
    if result.data:
        log_usage(context.user_data.get("user_id"), "note_added", f"contact_id={contact_id}")
        await update.message.reply_text(
            f"✓ Note saved to {contact_name}.\n\n"
            f"Use /list to see all, /find <name> to search."
        )
    else:
        await update.message.reply_text("Failed to save the note. Try again?")


async def post_init(application: Application) -> None:
    """Register the bot's command menu + start reminder scheduler."""
    commands = [
        BotCommand("start", "Welcome + onboarding"),
        BotCommand("save", "Add a new contact (name, handle, company, title, notes)"),
        BotCommand("list", "Show your 10 most recent contacts"),
        BotCommand("find", "Search contacts by name, company, or notes"),
        BotCommand("trip", "Start or end a trip to auto-tag new saves"),
        BotCommand("send", "Generate deep links to message a filtered group"),
        BotCommand("reminders", "Show your pending reminders"),
        BotCommand("help", "Show all commands and tips"),
        BotCommand("cancel", "Cancel the current operation"),
    ]
    await application.bot.set_my_commands(commands)
    log.info(f"Registered {len(commands)} commands in bot menu")

    # Start the reminder scheduler (60s loop)
    if application.job_queue is not None:
        application.job_queue.run_repeating(
            _reminder_tick,
            interval=60,
            first=10,
            name="reminder_scheduler",
        )
        log.info("Reminder scheduler started (60s interval)")
    else:
        log.warning("JobQueue not available; reminders will NOT auto-fire")


async def _reminder_tick(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Run every 60s. Find due reminders, send Telegram messages, mark fired."""
    from db import get_due_reminders, mark_reminder_fired, list_reminders

    try:
        due = get_due_reminders(limit=100)
    except Exception as e:
        log.exception("get_due_reminders failed")
        return

    if not due:
        return

    for r in due:
        try:
            user = r.get("user") or {}
            contact = r.get("contact") or {}
            telegram_uid = user.get("telegram_user_id")
            if not telegram_uid:
                log.warning("reminder %s has no telegram_user_id, skipping", r.get("id"))
                continue

            # Build the message
            recur_label = ""
            if r.get("recurrence") and r["recurrence"] != "none":
                recur_label = f"\nRecurring: {r['recurrence']}"
                if r.get("recurrence_end"):
                    recur_label += f" until {r['recurrence_end']}"

            contact_line = ""
            if contact:
                handle = contact.get("handle")
                company = contact.get("company")
                bits = [contact.get("name") or "contact"]
                if company:
                    bits.append(company)
                if handle:
                    bits.append(f"@{handle}")
                contact_line = f"\nContact: {' · '.join(bits)}"

            draft_line = ""
            if contact and contact.get("handle"):
                draft_line = (
                    f"\nDraft message: \"Hi {contact.get('name', '').split()[0] if contact.get('name') else 'there'}, "
                    f"following up — {r['message']}\""
                )

            text = (
                f"Reminder: {r['message']}"
                f"{contact_line}"
                f"{recur_label}"
                f"{draft_line}"
            )

            await context.bot.send_message(chat_id=telegram_uid, text=text)

            # Mark fired; for recurring this advances due_at to next occurrence
            next_due = mark_reminder_fired(r["id"])
            log.info(
                "Fired reminder %s for user %s (next_due=%s)",
                r["id"], telegram_uid, next_due,
            )
        except Exception as e:
            log.exception("Failed to fire reminder %s", r.get("id"))


async def reminders_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show user's pending + active recurring reminders."""
    from db import list_reminders as db_list
    user_id = context.user_data.get("user_id") or await _resolve_user_id(update, context)
    pending = db_list(user_id, status="pending", limit=20)
    if not pending:
        await update.message.reply_text("No pending reminders.")
        return
    lines = ["Your pending reminders:"]
    for r in pending:
        # Parse due_at for friendly display
        due = r["due_at"]
        contact_name = ""
        if r.get("contact"):
            contact_name = f" (linked to {r['contact'].get('name')})"
        recur = ""
        if r.get("recurrence") and r["recurrence"] != "none":
            recur = f" [{r['recurrence']}"
            if r.get("recurrence_end"):
                recur += f" until {r['recurrence_end']}"
            recur += "]"
        lines.append(f"\n- {r['message']}{contact_name}\n  Fires: {due}{recur}")
    await update.message.reply_text("\n".join(lines))


def main():
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    init_db()
    log.info("DB initialized")

    app = Application.builder().token(token).post_init(post_init).build()

    # Commands
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("save", save_cmd))
    app.add_handler(CommandHandler("list", list_cmd))
    app.add_handler(CommandHandler("find", find_cmd))
    app.add_handler(CommandHandler("trip", trip_cmd))
    app.add_handler(CommandHandler("send", send_cmd))
    app.add_handler(CommandHandler("reminders", reminders_cmd))
    app.add_handler(CommandHandler("cancel", cancel_save))
    app.add_handler(CommandHandler("signup", signup_cmd))
    app.add_handler(CommandHandler("stats", stats_cmd))

    # Messages
    app.add_handler(MessageHandler(filters.PHOTO, photo_handler))
    app.add_handler(MessageHandler(filters.CONTACT, contact_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, echo_text))

    log.info("Starting bot...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
