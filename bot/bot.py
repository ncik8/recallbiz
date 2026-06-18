"""RecallBiz Telegram bot — main entry point."""
import os
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
    get_or_create_user, get_filtered_contacts,
)


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

load_dotenv()
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=os.environ.get("LOG_LEVEL", "INFO"),
)
log = logging.getLogger(__name__)


WELCOME = """👋 Welcome to RecallBiz.

Your business card scanner + personal CRM, right inside Telegram.

📌 PIN THIS CHAT
Long-press in your chat list → "Pin". You'll want RecallBiz at the top during events.

📋 MENU
  /save  — add a contact manually
  /list  — your recent contacts
  /find  — search by name, company, notes
  /trip  — start/stop a trip (auto-tag who you meet)
  /help  — all commands + how-tips

Type / anytime to see this menu.

QUICK START
  1. /trip set <your event name>  ← starts auto-tagging
  2. /save to add contacts manually (QR auto-save coming soon)
  3. /find or /list to look them up"""

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

Filters for /send:
  tag:investor    — all contacts tagged X
  event:Token2049  — all contacts from event X (or current trip)
  trip:Token2049   — same as event:
  all              — every contact with a Telegram handle"""


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = await _resolve_user_id(update, context)
    log_usage(user_id, "start")
    await update.message.reply_text(WELCOME)


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP)


async def save_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Manual save flow — interactive prompts."""
    user_id = await _resolve_user_id(update, context)
    log_usage(user_id, "save_start")

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
    """If user is mid-save, route to save handler. Otherwise echo."""
    if context.user_data.get("save_step"):
        await _handle_save_message(update, context)
        return
    await update.message.reply_text(
        "I don't understand text yet. Use a command:\n"
        "/save · /list · /find · /trip · /help"
    )


async def photo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """For now, ack photo forwards — QR/OCR comes in Phase 2."""
    user_id = await _resolve_user_id(update, context)
    log_usage(user_id, "photo_received")
    await update.message.reply_text(
        "Got the image. QR + OCR auto-save is coming in the next update.\n\n"
        "For now, use /save to add contacts manually."
    )


async def post_init(application: Application) -> None:
    """Register the bot's command menu so it appears when user types /."""
    commands = [
        BotCommand("start", "Welcome + onboarding"),
        BotCommand("save", "Add a new contact (name, handle, company, title, notes)"),
        BotCommand("list", "Show your 10 most recent contacts"),
        BotCommand("find", "Search contacts by name, company, or notes"),
        BotCommand("trip", "Start or end a trip to auto-tag new saves"),
        BotCommand("send", "Generate deep links to message a filtered group"),
        BotCommand("help", "Show all commands and tips"),
        BotCommand("cancel", "Cancel the current operation"),
    ]
    await application.bot.set_my_commands(commands)
    log.info(f"Registered {len(commands)} commands in bot menu")


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
    app.add_handler(CommandHandler("cancel", cancel_save))

    # Messages
    app.add_handler(MessageHandler(filters.PHOTO, photo_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, echo_text))

    log.info("Starting bot...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
