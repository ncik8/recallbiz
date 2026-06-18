-- RecallBiz initial schema for Supabase (Postgres)
-- Run this in: Supabase Dashboard → SQL Editor → New Query → paste → Run
--
-- Multi-tenant with full row-level isolation. Each user sees only their own contacts,
-- tags, events, and usage logs. The bot uses the service_role key (server-side only)
-- and always filters by user_id explicitly — RLS is defense-in-depth.

-- ============================================================
-- 1. USERS — one row per Telegram user
-- ============================================================
CREATE TABLE IF NOT EXISTS users (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    telegram_user_id BIGINT UNIQUE NOT NULL,
    telegram_username TEXT,
    display_name TEXT,
    onboarded_at TIMESTAMPTZ DEFAULT now(),
    last_active_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_users_telegram ON users(telegram_user_id);

-- ============================================================
-- 2. CONTACTS — every person you save
-- ============================================================
CREATE TABLE IF NOT EXISTS contacts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    telegram_user_id BIGINT,             -- NULL for non-Telegram contacts (paper cards)
    name TEXT NOT NULL,
    handle TEXT,                          -- @username without @
    company TEXT,
    title TEXT,
    email TEXT,
    phone TEXT,
    notes TEXT,
    source TEXT NOT NULL DEFAULT 'manual', -- telegram_qr | manual | event_deeplink | paper_ocr
    saved_at TIMESTAMPTZ DEFAULT now(),
    last_contacted_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_contacts_user ON contacts(user_id);
CREATE INDEX IF NOT EXISTS idx_contacts_user_saved ON contacts(user_id, saved_at DESC);
CREATE INDEX IF NOT EXISTS idx_contacts_user_company ON contacts(user_id, company);

-- Full-text search via generated tsvector column + GIN index
ALTER TABLE contacts ADD COLUMN IF NOT EXISTS search_vector tsvector
    GENERATED ALWAYS AS (
        to_tsvector('english',
            coalesce(name, '') || ' ' ||
            coalesce(handle, '') || ' ' ||
            coalesce(company, '') || ' ' ||
            coalesce(title, '') || ' ' ||
            coalesce(notes, '')
        )
    ) STORED;
CREATE INDEX IF NOT EXISTS idx_contacts_search ON contacts USING GIN(search_vector);

-- ============================================================
-- 3. TAGS — free-form, scoped per user
-- ============================================================
CREATE TABLE IF NOT EXISTS tags (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    UNIQUE(user_id, name)
);
CREATE INDEX IF NOT EXISTS idx_tags_user ON tags(user_id);

-- ============================================================
-- 4. CONTACT_TAGS — many-to-many
-- ============================================================
CREATE TABLE IF NOT EXISTS contact_tags (
    contact_id UUID NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
    tag_id UUID NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
    PRIMARY KEY (contact_id, tag_id)
);
CREATE INDEX IF NOT EXISTS idx_contact_tags_tag ON contact_tags(tag_id);

-- ============================================================
-- 5. EVENTS / TRIPS — scoped per user
-- ============================================================
CREATE TABLE IF NOT EXISTS events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    slug TEXT NOT NULL,
    name TEXT NOT NULL,
    location TEXT,
    start_date DATE,
    end_date DATE,
    active BOOLEAN DEFAULT false,
    UNIQUE(user_id, slug)
);
CREATE INDEX IF NOT EXISTS idx_events_user ON events(user_id);
CREATE INDEX IF NOT EXISTS idx_events_user_active ON events(user_id, active) WHERE active = true;

-- ============================================================
-- 6. CONTACT_EVENTS — who did you meet where
-- ============================================================
CREATE TABLE IF NOT EXISTS contact_events (
    contact_id UUID NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
    event_id UUID NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    met_at TIMESTAMPTZ DEFAULT now(),
    context TEXT,
    PRIMARY KEY (contact_id, event_id)
);
CREATE INDEX IF NOT EXISTS idx_contact_events_event ON contact_events(event_id);

-- ============================================================
-- 7. USAGE — light audit log
-- ============================================================
CREATE TABLE IF NOT EXISTS usage (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID REFERENCES users(id) ON DELETE SET NULL,
    action TEXT NOT NULL,
    details TEXT,
    created_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_usage_user ON usage(user_id, created_at DESC);

-- ============================================================
-- 8. ROW LEVEL SECURITY — full isolation
-- ============================================================
ALTER TABLE contacts      ENABLE ROW LEVEL SECURITY;
ALTER TABLE tags          ENABLE ROW LEVEL SECURITY;
ALTER TABLE contact_tags  ENABLE ROW LEVEL SECURITY;
ALTER TABLE events        ENABLE ROW LEVEL SECURITY;
ALTER TABLE contact_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE usage         ENABLE ROW LEVEL SECURITY;

-- Drop existing policies if they exist (idempotent re-runs)
DROP POLICY IF EXISTS "Users see own contacts"        ON contacts;
DROP POLICY IF EXISTS "Users see own tags"            ON tags;
DROP POLICY IF EXISTS "Users see own contact_tags"    ON contact_tags;
DROP POLICY IF EXISTS "Users see own events"          ON events;
DROP POLICY IF EXISTS "Users see own contact_events"  ON contact_events;
DROP POLICY IF EXISTS "Users see own usage"           ON usage;

-- Direct ownership policies
CREATE POLICY "Users see own contacts" ON contacts
    FOR ALL TO authenticated
    USING (user_id = auth.uid())
    WITH CHECK (user_id = auth.uid());

CREATE POLICY "Users see own tags" ON tags
    FOR ALL TO authenticated
    USING (user_id = auth.uid())
    WITH CHECK (user_id = auth.uid());

CREATE POLICY "Users see own events" ON events
    FOR ALL TO authenticated
    USING (user_id = auth.uid())
    WITH CHECK (user_id = auth.uid());

CREATE POLICY "Users see own usage" ON usage
    FOR ALL TO authenticated
    USING (user_id = auth.uid());

-- M2M tables — check via parent ownership
CREATE POLICY "Users see own contact_tags" ON contact_tags
    FOR ALL TO authenticated
    USING (
        EXISTS (
            SELECT 1 FROM contacts
            WHERE contacts.id = contact_tags.contact_id
              AND contacts.user_id = auth.uid()
        )
    );

CREATE POLICY "Users see own contact_events" ON contact_events
    FOR ALL TO authenticated
    USING (
        EXISTS (
            SELECT 1 FROM contacts
            WHERE contacts.id = contact_events.contact_id
              AND contacts.user_id = auth.uid()
        )
    );

-- ============================================================
-- 9. SEED — common starter tags for each new user (via bot on /start)
-- Note: starter tags are NOT seeded here because they're per-user.
-- The bot inserts them when a new user row is created.
-- ============================================================

-- ============================================================
-- 10. HELPER FUNCTIONS (optional, for the bot)
-- ============================================================

-- Increment last_active_at on user activity (called from bot)
CREATE OR REPLACE FUNCTION touch_user(p_telegram_user_id BIGINT)
RETURNS UUID
LANGUAGE plpgsql
SECURITY DEFINER
AS $$
DECLARE
    v_user_id UUID;
BEGIN
    UPDATE users SET last_active_at = now()
    WHERE telegram_user_id = p_telegram_user_id
    RETURNING id INTO v_user_id;
    RETURN v_user_id;
END;
$$;

COMMENT ON TABLE users IS 'One row per Telegram user. Auth via telegram_user_id (treated as identity — Telegram IS the auth).';
COMMENT ON TABLE contacts IS 'Saved business contacts. Scoped per user via user_id. RLS enforces isolation.';
