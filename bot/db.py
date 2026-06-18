"""SQLite schema and helpers for RecallBiz."""
import sqlite3
from pathlib import Path
from contextlib import contextmanager


SCHEMA = """
-- The bot owner. Single row for v0.1.
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    telegram_user_id INTEGER UNIQUE NOT NULL,
    name TEXT,
    onboarded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- The core table. Every saved person.
CREATE TABLE IF NOT EXISTS contacts (
    id INTEGER PRIMARY KEY,
    telegram_user_id INTEGER UNIQUE,  -- NULL for non-Telegram contacts
    name TEXT NOT NULL,
    handle TEXT,                       -- @username (without @)
    company TEXT,
    title TEXT,
    email TEXT,
    phone TEXT,
    notes TEXT,
    source TEXT NOT NULL,              -- 'telegram_qr', 'manual', 'event_deeplink', 'paper_ocr'
    saved_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_contacted_at TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_contacts_saved_at ON contacts(saved_at DESC);
CREATE INDEX IF NOT EXISTS idx_contacts_company ON contacts(company);

-- Tags (free-form)
CREATE TABLE IF NOT EXISTS tags (
    id INTEGER PRIMARY KEY,
    name TEXT UNIQUE NOT NULL
);

-- Many-to-many: contact ↔ tag
CREATE TABLE IF NOT EXISTS contact_tags (
    contact_id INTEGER NOT NULL,
    tag_id INTEGER NOT NULL,
    PRIMARY KEY (contact_id, tag_id),
    FOREIGN KEY (contact_id) REFERENCES contacts(id) ON DELETE CASCADE,
    FOREIGN KEY (tag_id) REFERENCES tags(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_contact_tags_tag ON contact_tags(tag_id);

-- Events / trips
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    slug TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    location TEXT,
    start_date DATE,
    end_date DATE,
    active BOOLEAN DEFAULT 0
);

-- Many-to-many: contact ↔ event
CREATE TABLE IF NOT EXISTS contact_events (
    contact_id INTEGER NOT NULL,
    event_id INTEGER NOT NULL,
    met_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    context TEXT,
    PRIMARY KEY (contact_id, event_id),
    FOREIGN KEY (contact_id) REFERENCES contacts(id) ON DELETE CASCADE,
    FOREIGN KEY (event_id) REFERENCES events(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_contact_events_event ON contact_events(event_id);

-- FTS5 search
CREATE VIRTUAL TABLE IF NOT EXISTS contacts_fts USING fts5(
    name, handle, company, title, notes,
    content='contacts', content_rowid='id'
);

-- Light usage logging
CREATE TABLE IF NOT EXISTS usage (
    id INTEGER PRIMARY KEY,
    action TEXT NOT NULL,
    details TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""


def get_db_path() -> Path:
    from dotenv import load_dotenv
    import os
    load_dotenv()
    p = Path(os.environ.get("DATABASE_PATH", "data/recallbiz.db"))
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


@contextmanager
def get_conn():
    db_path = get_db_path()
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    """Create tables if they don't exist. Idempotent."""
    with get_conn() as conn:
        conn.executescript(SCHEMA)
        # Seed starter tags
        starter_tags = ["investor", "founder", "speaker", "team", "press"]
        for t in starter_tags:
            conn.execute(
                "INSERT OR IGNORE INTO tags (name) VALUES (?)",
                (t,)
            )


def log_usage(action: str, details: str = None):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO usage (action, details) VALUES (?, ?)",
            (action, details)
        )


def save_contact(
    name: str,
    handle: str = None,
    company: str = None,
    title: str = None,
    email: str = None,
    phone: str = None,
    notes: str = None,
    source: str = "manual",
    telegram_user_id: int = None,
) -> int:
    """Insert contact + return new ID."""
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO contacts
               (telegram_user_id, name, handle, company, title, email, phone, notes, source)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (telegram_user_id, name, handle, company, title, email, phone, notes, source)
        )
        new_id = cur.lastrowid
        # Update FTS index
        conn.execute(
            "INSERT INTO contacts_fts(rowid, name, handle, company, title, notes) VALUES (?, ?, ?, ?, ?, ?)",
            (new_id, name, handle or "", company or "", title or "", notes or "")
        )
        return new_id


def search_contacts(query: str, limit: int = 10):
    """FTS5 search across name, handle, company, title, notes."""
    if not query.strip():
        return []
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT c.* FROM contacts c
               JOIN contacts_fts f ON c.id = f.rowid
               WHERE contacts_fts MATCH ?
               ORDER BY rank
               LIMIT ?""",
            (query, limit)
        ).fetchall()
        return [dict(r) for r in rows]


def list_recent(limit: int = 10):
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM contacts ORDER BY saved_at DESC LIMIT ?",
            (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


def set_active_trip(event_name: str) -> int:
    """Mark all events inactive, create/find the named event, mark active. Return event ID."""
    with get_conn() as conn:
        conn.execute("UPDATE events SET active = 0")
        slug = event_name.lower().replace(" ", "_").replace("/", "-")
        row = conn.execute("SELECT id FROM events WHERE slug = ?", (slug,)).fetchone()
        if row:
            event_id = row["id"]
        else:
            cur = conn.execute(
                "INSERT INTO events (slug, name, active) VALUES (?, ?, 1)",
                (slug, event_name)
            )
            event_id = cur.lastrowid
        conn.execute("UPDATE events SET active = 1 WHERE id = ?", (event_id,))
        return event_id


def get_active_trip():
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM events WHERE active = 1 ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None


if __name__ == "__main__":
    init_db()
    print("DB initialized at", get_db_path())
