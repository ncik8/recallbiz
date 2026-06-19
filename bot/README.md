# TRCE bot (formerly RecallBiz)

Telegram-native business card scanner + personal CRM. Never forget a contact again.

**Bot handle:** @trceiobot
**Status:** v0.1 — manual save + trip mode + search
**Stack:** Python 3.11, python-telegram-bot v21, SQLite, Railway

## Setup (local)

```bash
cd /Users/nick/recallbiz/bot
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Token already in .env. Verify it works:
python3 -c "from db import init_db; init_db(); print('OK')"

# Run the bot (long-polling for local dev)
python3 bot.py
```

## Setup (Railway)

Push to GitHub, connect Railway, set:
- `TELEGRAM_BOT_TOKEN` (from .env)
- `DATABASE_PATH=/data/recallbiz.db` (Railway persistent volume)
- Volume mount: `/data` for SQLite persistence

## Commands (v0.1)

- `/start` — Welcome + onboarding
- `/save` — Manual save (5-step flow: name, handle, company, title, notes)
- `/list` — Last 10 contacts
- `/find <query>` — FTS5 search
- `/trip set <name>` — Start a trip (auto-tags saves)
- `/trip off` — Stop tagging
- `/trip` — Show current trip
- `/help` — List commands

## Files

- `bot.py` — Main entry, command handlers
- `db.py` — SQLite schema + helpers (init_db, save_contact, search_contacts, etc.)
- `.env` — Secrets (NEVER commit)
- `.gitignore` — Excludes .env, *.db, venv

## Roadmap

- **v0.1 (now):** Manual save, trip mode, search
- **v0.2:** QR forward → save via getChat, paper card OCR
- **v0.3:** Deep link event context (`?start=event_<slug>`)
- **v0.4:** Web dashboard + Telegram Login Widget
- **v0.5:** GameFi (Missile Command mini-app, lives currency, daily tasks)
- **v0.6:** Multi-game library + Telegram Stars payments

See `../SKETCH.md` for full architecture + schema design.
