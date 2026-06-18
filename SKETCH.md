# recallbiz — Technical Sketch

**Drafted:** 2026-06-17 (Wed)
**First beta event:** Tuesday 2026-06-23 (HK crypto meetup, 6 days)
**Cadence:** bi-weekly local events after that
**Launch target:** Token2049 Singapore, Sep 18-20 2026 (~12 weeks)
**Author:** Henry (with Nick's approval)

---

## TL;DR

A Telegram-native business card scanner + personal CRM. **For v0.1 (Tuesday 6/23)**, ship the absolute minimum: forward a Telegram QR to the bot, bot saves the contact via `getChat`, list recent, find by name/company/event. Everything else (OCR paper cards, follow-up drafts, sendContact share, batch mode, multi-user) is post-MVP.

**v0.1 scope is single-user (Nick only).** Other attendees don't need accounts — they just forward their Telegram QR to the bot when they share with Nick. The viral sendContact share comes in v0.2.

---

## Bot identity

**Username:** `RecallBizBot` (capital R, B — Telegram handles are case-insensitive in URLs but this is the canonical form per @BotFather)
**Display name:** RecallBiz
**Token location:** `/Users/nick/recallbiz/bot/.env` (never commit, will add to .gitignore)
**Stack:** Python 3.11, python-telegram-bot v21, SQLite (file-based), Railway deploy
**No web UI** — Telegram IS the interface. Web UI is the dashboard app at app.recallbiz.xyz (post-MVP).

---

## Commands (v0.1)

| Command | Purpose |
|---|---|
| `/start` | Welcome + onboard (just stores Nick's `telegram_user_id`) |
| `/save` | Manual entry: `name, handle, company, title, notes` — interactive prompts |
| `/list` | Show last 10 contacts (paginated if >10) |
| `/find <query>` | Search by name, company, title, notes, event, tag |
| `/trip set <name>` | Set active trip (auto-tags all saves) |
| `/trip on/off` | Quick toggle (without changing the trip name) |
| `/trip` | Show current trip + count of contacts saved this trip |
| `/help` | Command list |

**Inline flows (no command needed):**
- **Forward any QR image** → bot detects Telegram QR (via image hash + `getChat`), confirms, saves
- **Forward any photo** → bot asks "Is this a business card?" → if yes, OCR via M3 vision (v0.2)

**Inline buttons (Telegram UI):**
- After save: `[✓ Saved] [Edit] [Add tag] [Cancel]`
- After find: `[View details] [Message them] [Draft follow-up]` (last two are v0.2)

---

## SQLite schema (v0.1)

```sql
-- The bot owner. Single row for v0.1.
CREATE TABLE users (
    id INTEGER PRIMARY KEY,
    telegram_user_id INTEGER UNIQUE NOT NULL,
    name TEXT,
    onboarded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- The core table. Every saved person.
CREATE TABLE contacts (
    id INTEGER PRIMARY KEY,
    telegram_user_id INTEGER UNIQUE,  -- NULL for non-Telegram contacts
    name TEXT NOT NULL,
    handle TEXT,                       -- @username (without @)
    company TEXT,
    title TEXT,
    email TEXT,
    phone TEXT,
    notes TEXT,
    source TEXT NOT NULL,              -- 'telegram_qr', 'manual', 'event_deeplink', 'paper_ocr' (v0.2)
    saved_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_contacted_at TIMESTAMP
);
CREATE INDEX idx_contacts_saved_at ON contacts(saved_at DESC);
CREATE INDEX idx_contacts_company ON contacts(company);

-- Tags (free-form). Seeded with starter tags.
CREATE TABLE tags (
    id INTEGER PRIMARY KEY,
    name TEXT UNIQUE NOT NULL,
    color TEXT                         -- for UI badge (post-MVP)
);

-- Many-to-many: contact ↔ tag
CREATE TABLE contact_tags (
    contact_id INTEGER NOT NULL,
    tag_id INTEGER NOT NULL,
    PRIMARY KEY (contact_id, tag_id),
    FOREIGN KEY (contact_id) REFERENCES contacts(id) ON DELETE CASCADE,
    FOREIGN KEY (tag_id) REFERENCES tags(id) ON DELETE CASCADE
);
CREATE INDEX idx_contact_tags_tag ON contact_tags(tag_id);

-- Events / trips (e.g. "Token2049 Singapore Sep 2026")
CREATE TABLE events (
    id INTEGER PRIMARY KEY,
    slug TEXT UNIQUE NOT NULL,         -- 'token2049_2026', 'hk_meetup_2026_06_23'
    name TEXT NOT NULL,
    location TEXT,
    start_date DATE,
    end_date DATE,
    active BOOLEAN DEFAULT 0           -- currently the "trip" being tagged
);

-- Many-to-many: contact ↔ event (where we met them)
CREATE TABLE contact_events (
    contact_id INTEGER NOT NULL,
    event_id INTEGER NOT NULL,
    met_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    context TEXT,                      -- 'booth', 'talk', 'afterparty', 'panel', etc.
    PRIMARY KEY (contact_id, event_id),
    FOREIGN KEY (contact_id) REFERENCES contacts(id) ON DELETE CASCADE,
    FOREIGN KEY (event_id) REFERENCES events(id) ON DELETE CASCADE
);
CREATE INDEX idx_contact_events_event ON contact_events(event_id);

-- FTS5 for /find queries (fast search across name, company, title, notes)
CREATE VIRTUAL TABLE contacts_fts USING fts5(
    name, handle, company, title, notes,
    content='contacts', content_rowid='id'
);

-- Triggers to keep FTS in sync
CREATE TRIGGER contacts_ai AFTER INSERT ON contacts BEGIN
    INSERT INTO contacts_fts(rowid, name, handle, company, title, notes)
    VALUES (new.id, new.name, new.handle, new.company, new.title, new.notes);
END;
CREATE TRIGGER contacts_ad AFTER DELETE ON contacts BEGIN
    INSERT INTO contacts_fts(contacts_fts, rowid, name, handle, company, title, notes)
    VALUES ('delete', old.id, old.name, old.handle, old.company, old.title, old.notes);
END;
CREATE TRIGGER contacts_au AFTER UPDATE ON contacts BEGIN
    INSERT INTO contacts_fts(contacts_fts, rowid, name, handle, company, title, notes)
    VALUES ('delete', old.id, old.name, old.handle, old.company, old.title, old.notes);
    INSERT INTO contacts_fts(rowid, name, handle, company, title, notes)
    VALUES (new.id, new.name, new.handle, new.company, new.title, new.notes);
END;

-- Light usage logging (for "what did I do this week")
CREATE TABLE usage (
    id INTEGER PRIMARY KEY,
    action TEXT NOT NULL,              -- 'save', 'find', 'list', 'trip_set', etc.
    details TEXT,                      -- JSON
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

**Storage math:** 1 contact ≈ 500 bytes. 1,000 contacts ≈ 500 KB. 100,000 contacts (the "500 from Token2049" worst case × 200 events) = 50 MB. SQLite handles this trivially.

---

## Core flows (v0.1)

### Flow 1: Onboarding (`/start`)

```
User: /start
Bot: Welcome to recallbiz 📇
     Scan a business card, save a contact, find anyone you met.
     I'll keep track of who you met and where.
     Try it: forward me a Telegram QR code image.
```

Single-user MVP — no separate account, just stores Nick's telegram_user_id on first /start.

### Flow 2: Save via Telegram QR forward (the killer feature)

```
User: [forwards image of someone's Telegram QR]
Bot: I see a Telegram QR. Scanning...
     Found: @johnsmith (John Smith)
     [Save] [Not this person] [Cancel]
User: [taps Save]
Bot: ✓ Saved John Smith (@johnsmith)
     Active trip: Token2049 Singapore
     Tagged with: Token2049 Singapore
     [View] [Add tag] [Draft follow-up] (v0.2)
```

How: bot downloads image → checks for QR pattern (pyzbar or similar) → extracts `t.me/username` or QR payload → calls `getChat` to resolve user info → confirms → saves.

For QRs that point to phone numbers or usernames not in Telegram: parse what we can, ask user for missing fields.

### Flow 3: Manual save (`/save`)

```
User: /save
Bot: Name?
User: Jane Doe
Bot: Handle? (@username or skip)
User: @janedoe
Bot: Company?
User: Polychain
Bot: Title?
User: Partner
Bot: Notes? (or /skip)
User: Met at booth, talking about L2 sequencers
Bot: ✓ Saved Jane Doe (@janedoe), Partner at Polychain
     Tagged with: Token2049 Singapore (active trip)
     [View] [Add tag] [Draft follow-up] (v0.2)
```

State machine: store partial input in memory or Redis-equivalent (for v0.1 just keep in bot context; users re-issue if they get lost). After save, auto-tag with active trip if set.

### Flow 4: Find (`/find`)

```
User: /find polychain
Bot: 2 contacts at Polychain:
     • Jane Doe, Partner — met at Token2049 (afterparty)
     • Bob Lee, Investor — met at Token2049 (booth)
     [View all] [Refine search]
```

Uses FTS5 for fast matching. Searches name, handle, company, title, notes. Also matches events and tags.

### Flow 5: List (`/list`)

```
User: /list
Bot: Your last 10 contacts:
     1. Jane Doe (@janedoe) — Polychain · 2h ago · Token2049
     2. Bob Lee (@boblee) — Polychain · 3h ago · Token2049
     3. ...
     [Next 10] [Filter by trip] [Search]
```

Sorted by `saved_at DESC`. Pagination 10 per page.

### Flow 6: Trip mode

```
User: /trip set Token2049 Singapore
Bot: ✓ Trip set: Token2049 Singapore
     All new saves auto-tagged until /trip off.

User: /trip
Bot: Current trip: Token2049 Singapore
     Saved this trip: 12 contacts
     Started: 2 hours ago

User: /trip off
Bot: ✓ Trip mode off. New saves not tagged.
```

Saves the active event name. On every subsequent save, auto-insert into `contact_events` with the active event_id. Toggle without changing name with /trip off.

---

## Trip mode + deep link spec

**Trip mode** (above) — manual /trip set/on/off. Auto-tags saves with active event.

**Deep links** (post-MVP, low effort — add for v0.2):
- Pattern: `t.me/RecallBizBot?start=event_<slug>`
- Event organizer includes this in welcome email / Telegram channel pin
- Attendee clicks → bot: "Welcome from Token2049! Quick check-in: where did you meet them?" with buttons [Booth] [Talk] [Afterparty] [Other]
- Pre-fills the active trip + offers to save with one tap if they forward their contact's QR

Both feed the same `events` + `contact_events` tables. Deep link just bypasses the manual /trip set step.

---

## M3 prompt patterns (for reference, v0.2+)

**Paper card OCR (v0.2):**
```
System: Extract structured contact info from a business card image.
Return JSON only: {"name": str, "title": str, "company": str,
  "email": str, "phone": str, "telegram_handle": str|null, "website": str|null}
Omit fields not visible. No commentary.
User: [attached image]
```

**Follow-up draft (v0.2):**
```
System: Draft a 3-sentence follow-up after a conference meeting.
Reference the notes. Friendly, specific, no flattery.
User: Contact: {name}, {title} at {company}. Met at {event}.
Notes from meeting: {notes}. Today's date: {date}.
```

**Query expansion (v0.2 — for fuzzy /find):**
```
System: User is searching contacts. Expand their query to FTS5-friendly terms.
User query: "that person from Polychain I met after the sequencer talk"
Output JSON: {"terms": ["Polychain", "sequencer", "after"], "filters": {"event": null}, "intent": "find_contact"}
```

For v0.1, /find uses straight FTS5 — no M3 round-trip needed. Faster, cheaper, simpler.

---

## MVP sprint — 6 days to Tuesday 6/23

| Day | Owner | Deliverable |
|---|---|---|
| **Wed 6/17 (today)** | Nick | Create bot via @BotFather, share token with Henry |
| | Henry | Repo skeleton + Railway deploy skeleton + SQLite schema migrations |
| **Thu 6/18** | Henry | /start, /help, /save (full manual flow), /list (basic) |
| **Fri 6/19** | Henry | QR forward flow: pyzbar detection → getChat → save |
| | Nick | Test with 5 of Nick's existing Telegram contacts |
| **Sat 6/20** | Henry | /trip set/on/off, auto-tag on save |
| | Nick | Polish: send Henry 3 example "this is how I'd use it" flows |
| **Sun 6/21** | Henry | /find (FTS5 search) |
| | Nick | Onboard with Henry via 1:1 test, log any UX papercuts |
| **Mon 6/22** | Henry | Bug bash + Railway prod deploy |
| | Nick | Rehearse demo flow (forward QR → save → find by company) |
| **Tue 6/23** | Nick | **Event day. Bring phone. Forward every QR.** |
| **Wed 6/24** | Nick + Henry | Debrief: what worked, what broke, what to fix before next event (6/30 or 7/7) |

**Success metric for 6/23:** Nick saves 10+ real contacts during the event without any friction >30 seconds. If we hit that, MVP is real. If not, we know exactly what to fix before the next event.

---

## Post-event: 4-week MVP+ plan

| Week | Build | Outcome |
|---|---|---|
| **W1 (6/24-6/30)** | OCR via M3 vision (paper cards) | Photo forward → "Save this card?" → done |
| | BotFather inline buttons for source selection | [Telegram QR] [Paper card] [Manual] on photo forward |
| **W2 (7/1-7/7)** | Deep link event context (`?start=event_<slug>`) | Organizer distributes, attendees one-tap save |
| | Event creation UI in chat: `/event add Token2049 Singapore Sep 18-20` | |
| **W3 (7/8-7/14)** | M3 follow-up drafts (post-event: "draft follow-ups for Token2049 batch") | Pro tier anchor feature |
| **W4 (7/15-7/21)** | sendContact share (recipient gets native vCard via Telegram — viral loop) | Pro tier differentiation |
| | Telegram Stars payments integration for $9.99/mo Pro | |
| **Then** | Token2049 Singapore launch prep | Public bot, marketing, Pro tier live |

After W4 → public launch. If Token2049 is the public launch, we ship Pro tier + sendContact viral loop before Sep 18.

---

## Open questions (need Nick's input)

1. **Bot token** — Nick creates via @BotFather, shares token. Bot handle = `recallbiz_bot`?
2. **Multi-user in v0.1?** I assumed single-user (Nick only). Other attendees don't onboard — they just forward their QR. Confirm?
3. **Telegram group vs DM flow** — when Nick forwards a QR from a Telegram group, do we save the sender (the person who sent the QR) or the QR target (the person in the QR)? Likely the latter (the QR target is who Nick is "meeting").
4. **Privacy / data export** — should /export be in v0.1? Probably yes (CSV download) — gives Nick an escape hatch if he wants to switch later. Low effort to add.
5. **Cost expectations** — for 1 event with 10-30 saves, M3 cost is negligible. For Token2049 with 500+ saves, M3 ≈ $0.50. Confirmed safe.
6. **Where to host?** Railway (recommended for 6-day deadline — just push and deploy), or local Mac + tunnel (Nick's usual preference)? Telegram bot has no local Mac file dependency, so Railway wins for v0.1. Switch later if needed.

---

## Files for reference

- `~/recallbiz/README.md` — original concept + pricing + features
- `~/recallbiz/competitor-pricing.md` — competitive landscape
- `~/recallbiz/SKETCH.md` — this file (technical design)

---

**My pick on next move:** wait for Nick's bot token, then start the repo + Railway deploy skeleton today (Wed 6/17). Tomorrow (Thu) start on /start + /save. By Sun 6/21 we have /find working. Mon 6/22 is bug bash + rehearsal. Tue 6/23 = event.

If we slip: drop /find from MVP. Forward + save + trip mode alone = enough for a useful demo at the event.
