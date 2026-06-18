-- ============================================================
-- RecallBiz: run ALL migrations (idempotent)
-- Paste this once into Supabase SQL Editor -> Run.
-- Order matters: 001 first (creates users), then 002s, 003, 004, 005.
-- ============================================================


-- ============================================================
-- 001_initial_schema.sql
-- ============================================================
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


-- ============================================================
-- 002_fix_search_vector.sql
-- ============================================================
-- Migration 002: Fix search_vector column on contacts
--
-- Issue: The original 001_initial_schema.sql had a syntax error in the
-- GENERATED ALWAYS AS expression — adjacent string literals need ||
-- between them in Postgres (e.g. `coalesce(name, '') || ' ' ||`).
-- Without ||, Postgres would throw a syntax error on creation.
--
-- But the column IS in the schema (your query confirmed this). Possible
-- explanations:
--   a) Postgres created the column with an empty/broken expression
--   b) Adjacent literals were concatenated differently than I expected
--   c) The IF NOT EXISTS clause masked a partial failure
--
-- Either way: this migration drops + recreates the column with the
-- correct expression. All 5 fields are included.
--
-- Run in: Supabase Dashboard → SQL Editor → New query → paste → Run

-- Drop the existing column (CASCADE to also drop the GIN index)
ALTER TABLE contacts DROP COLUMN IF EXISTS search_vector CASCADE;

-- Recreate with proper || operators
ALTER TABLE contacts ADD COLUMN search_vector tsvector
    GENERATED ALWAYS AS (
        to_tsvector('english',
            coalesce(name, '') || ' ' ||
            coalesce(handle, '') || ' ' ||
            coalesce(company, '') || ' ' ||
            coalesce(title, '') || ' ' ||
            coalesce(notes, '')
        )
    ) STORED;

-- Recreate the GIN index for fast full-text search
CREATE INDEX IF NOT EXISTS idx_contacts_search ON contacts USING GIN(search_vector);

-- ============================================================
-- VERIFICATION — run this AFTER the migration to confirm it's correct:
-- ============================================================
-- SELECT generation_expression
-- FROM information_schema.columns
-- WHERE table_name = 'contacts' AND column_name = 'search_vector';
--
-- Expected: should show "to_tsvector('english'::regconfig, ...)" with
-- all 5 fields (name, handle, company, title, notes).
--
-- FUNCTIONAL TEST — insert + query a test row (replace USER_ID with
-- a real UUID from your users table):
--
-- INSERT INTO contacts (user_id, name, company, handle, source)
-- VALUES ('YOUR_USER_UUID_HERE', 'Vitalik Buterin', 'Ethereum', 'vitalik', 'test');
--
-- SELECT name, company FROM contacts
-- WHERE search_vector @@ to_tsquery('english', 'vitalik');
--
-- Expected: returns the row you just inserted.


-- ============================================================
-- 002_website_and_fts.sql
-- ============================================================
-- Migration 002: Add website column + rebuild search_vector with proper concatenation
--
-- Why this exists:
-- 001_initial_schema.sql had a typo: `coalesce(name, '') ' '` (missing ||).
-- Postgres silently ignored the broken tsvector column definition or created
-- a partial one. The UI's "generation_expression" field shows it was fixed
-- to use || at some point, but Postgres does NOT auto-recompute STORED
-- generated columns when the definition changes — only on INSERT/UPDATE.
--
-- This migration:
-- 1. Drops + recreates search_vector with correct || operators (triggers rebuild)
-- 2. Adds website column to contacts
-- 3. Re-creates the GIN index

-- Step 1: Add website column
ALTER TABLE contacts ADD COLUMN IF NOT EXISTS website TEXT;

-- Step 2: Drop + recreate search_vector (Postgres recomputes on ADD)
ALTER TABLE contacts DROP COLUMN IF EXISTS search_vector;

ALTER TABLE contacts ADD COLUMN search_vector tsvector
    GENERATED ALWAYS AS (
        to_tsvector('english',
            coalesce(name, '') || ' ' ||
            coalesce(handle, '') || ' ' ||
            coalesce(company, '') || ' ' ||
            coalesce(title, '') || ' ' ||
            coalesce(email, '') || ' ' ||
            coalesce(phone, '') || ' ' ||
            coalesce(notes, '') || ' ' ||
            coalesce(website, '')
        )
    ) STORED;

-- Step 3: Re-create the GIN index
CREATE INDEX IF NOT EXISTS idx_contacts_search ON contacts USING GIN(search_vector);

-- Verification (run this after migration):
-- SELECT name, company, search_vector IS NOT NULL AS has_vector
-- FROM contacts ORDER BY saved_at DESC LIMIT 5;


-- ============================================================
-- 003_reminders.sql
-- ============================================================
-- Migration 003: Reminders + user timezone
-- Run in: Supabase Dashboard → SQL Editor → New query → Cmd+Enter

-- ============================================================
-- 1. Add timezone to users (nullable, set on first reminder)
-- ============================================================
ALTER TABLE users
  ADD COLUMN IF NOT EXISTS timezone TEXT;

COMMENT ON COLUMN users.timezone IS
  'IANA timezone like Asia/Hong_Kong. NULL until user sets first reminder.';

-- ============================================================
-- 2. Reminders table
-- ============================================================
CREATE TABLE IF NOT EXISTS reminders (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,

    -- Optional link to a contact (so we can pre-fill "send to @vitalik")
    contact_id UUID REFERENCES contacts(id) ON DELETE SET NULL,

    message TEXT NOT NULL,

    -- When to fire. timestamptz so timezone offset is stored explicitly.
    due_at TIMESTAMPTZ NOT NULL,
    timezone TEXT NOT NULL,  -- IANA timezone at creation time (for display)

    -- Lifecycle
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'done', 'snoozed', 'cancelled')),
    fired_at TIMESTAMPTZ,  -- last time we delivered (NULL until fired)
    last_fired_at TIMESTAMPTZ,  -- alias for recurring logic

    -- Recurrence: 'none' (one-shot), 'daily', 'weekly', 'monthly'
    recurrence TEXT NOT NULL DEFAULT 'none'
        CHECK (recurrence IN ('none', 'daily', 'weekly', 'monthly')),
    recurrence_end DATE,  -- NULL = no end; otherwise stop after this date
    next_due_at TIMESTAMPTZ,  -- for recurring: when to fire next; NULL for one-shot

    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- For recurring reminders, this is the parent reminder that owns the recurrence.
    -- NULL for one-shots OR for the first occurrence of a recurring series.
    parent_id UUID REFERENCES reminders(id) ON DELETE CASCADE
);

COMMENT ON TABLE reminders IS
  'One-shot or recurring reminders set by the user via the bot.';

COMMENT ON COLUMN reminders.due_at IS
  'Next firing time. For recurring, updated to next occurrence after each fire.';
COMMENT ON COLUMN reminders.recurrence IS
  'none (one-shot) / daily / weekly / monthly. After firing, due_at advances by interval.';
COMMENT ON COLUMN reminders.parent_id IS
  'For recurring: links child firings back to the original. NULL for one-shot.';

-- ============================================================
-- 3. Indexes for the 60s scheduler
-- ============================================================

-- Scheduler query: WHERE status='pending' AND due_at <= NOW()
CREATE INDEX IF NOT EXISTS idx_reminders_due
    ON reminders (due_at)
    WHERE status = 'pending';

-- User dashboard / /reminders listing
CREATE INDEX IF NOT EXISTS idx_reminders_user_pending
    ON reminders (user_id, due_at)
    WHERE status = 'pending';

-- ============================================================
-- 4. Row Level Security — simplified
-- ============================================================
-- Bot uses service_role key which bypasses RLS, so it can manage
-- reminders for any user. The app-layer in db.py filters by user_id
-- on every query. RLS here is defense-in-depth for the REST API (anon key).
--
-- NOTE: the previous RLS policies used a comparison between users.telegram_user_id
-- (bigint) and JWT claim text, which Postgres rejected with 42883.
-- Simpler approach: drop RLS, app-layer filtering is sufficient for v0.1.
-- Re-enable with explicit casting when adding a public REST endpoint.
ALTER TABLE reminders ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "Users see own reminders" ON reminders;
DROP POLICY IF EXISTS "Users insert own reminders" ON reminders;
DROP POLICY IF EXISTS "Users update own reminders" ON reminders;
DROP POLICY IF EXISTS "Users delete own reminders" ON reminders;

-- Permissive policy: deny all by default, service_role bypasses.
-- When you add a user-facing REST endpoint, write a policy that casts
-- the JWT claim to bigint: ((current_setting('request.jwt.claims', true)::json->>'telegram_user_id')::bigint)
DROP POLICY IF EXISTS "Service role manages all reminders" ON reminders;
CREATE POLICY "Service role manages all reminders"
    ON reminders FOR ALL
    TO service_role
    USING (true)
    WITH CHECK (true);

DROP POLICY IF EXISTS "Anon read only own reminders" ON reminders;
CREATE POLICY "Anon read only own reminders"
    ON reminders FOR SELECT
    TO anon
    USING (false);  -- Will be replaced when user auth is wired

-- ============================================================
-- 5. touch_user updated_at trigger already covers users (001).
-- ============================================================


-- ============================================================
-- 004_chat_history.sql
-- ============================================================
-- Migration 004: Chat history (persistent conversation context)
-- Run in: Supabase Dashboard → SQL Editor → New query → Cmd+Enter
--
-- Why: the bot's conversational AI needs to see the last few turns to
-- avoid re-asking for info the user already gave (e.g. timezone, contact
-- name). Without history, each turn is a fresh LLM call with zero memory.
--
-- Schema: minimal — just role + content + timestamp. The AI re-runs its
-- own tool calls each turn, so we don't replay tool_call_id metadata.

CREATE TABLE IF NOT EXISTS messages (
    id BIGSERIAL PRIMARY KEY,
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role TEXT NOT NULL CHECK (role IN ('user', 'assistant', 'tool')),
    content TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE messages IS
  'Persistent per-user chat history. Loaded as the last N messages before each LLM call so the AI has cross-turn context.';

COMMENT ON COLUMN messages.role IS
  'OpenAI-style role: user (human), assistant (bot reply), tool (function result).';

-- Hot path: get_recent_messages(user_id) ORDER BY created_at DESC LIMIT 10
CREATE INDEX IF NOT EXISTS idx_messages_user_created
    ON messages (user_id, created_at DESC);

-- ============================================================
-- Row Level Security — same pattern as 003 reminders
-- ============================================================
-- service_role bypasses RLS and is what the bot uses. The app-layer
-- in db.py filters by user_id on every query. RLS here is
-- defense-in-depth for the REST API (anon key).
ALTER TABLE messages ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "Service role manages all messages" ON messages;
DROP POLICY IF EXISTS "Anon read only own messages" ON messages;

CREATE POLICY "Service role manages all messages"
    ON messages FOR ALL
    TO service_role
    USING (true)
    WITH CHECK (true);

CREATE POLICY "Anon read only own messages"
    ON messages FOR SELECT
    TO anon
    USING (false);  -- Will be replaced when user auth is wired

-- ============================================================
-- Optional: prune old messages (run as cron if storage matters)
-- ============================================================
-- For now we keep history forever. If storage becomes a concern, add:
--   DELETE FROM messages WHERE created_at < NOW() - INTERVAL '30 days';
-- on a daily cron.


-- ============================================================
-- 005_auth_and_tiers.sql
-- ============================================================
-- Migration 005: Email signup (magic link) + tier system + tester designation
-- Run in: Supabase Dashboard → SQL Editor → New query → Cmd+Enter
--
-- Why: the bot needs a real user identity beyond telegram_user_id so we can
-- (a) count signups for the funding metric, (b) gate free users at 10 contacts,
-- (c) grandfather testers as unlimited.
--
-- Magic link flow: user types /signup email@x.com -> bot sends email via Resend
-- with a t.me/RecallBizBot?start=verify_<token> link -> user clicks -> bot
-- verifies and marks email_verified=true.

-- ============================================================
-- 1. Extend users table: email, verified, tester, plan
-- ============================================================
ALTER TABLE users
    ADD COLUMN IF NOT EXISTS email TEXT UNIQUE,
    ADD COLUMN IF NOT EXISTS email_verified BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS is_tester BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS plan TEXT NOT NULL DEFAULT 'free'
        CHECK (plan IN ('free', 'pro', 'team', 'tester'));

COMMENT ON COLUMN users.email IS
    'Verified email captured via /signup magic link. NULL until signup completes.';
COMMENT ON COLUMN users.email_verified IS
    'True once the user clicked the magic link in their inbox.';
COMMENT ON COLUMN users.is_tester IS
    'True for the 2-3 friends testing unlimited. Set manually via SQL: UPDATE users SET is_tester=TRUE WHERE telegram_user_id=...;';
COMMENT ON COLUMN users.plan IS
    'free (10 contacts cap) | pro (unlimited) | team (unlimited + collaboration) | tester (unlimited, no Stripe billing).';

-- ============================================================
-- 2. Magic link tokens
-- ============================================================
CREATE TABLE IF NOT EXISTS magic_link_tokens (
    token TEXT PRIMARY KEY,
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    email TEXT NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL DEFAULT (NOW() + INTERVAL '15 minutes'),
    used_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE magic_link_tokens IS
    'One-time tokens for /signup email verification. Expires in 15 min. Marked used_at on consumption.';

CREATE INDEX IF NOT EXISTS idx_magic_tokens_user
    ON magic_link_tokens (user_id);

CREATE INDEX IF NOT EXISTS idx_magic_tokens_active
    ON magic_link_tokens (expires_at)
    WHERE used_at IS NULL;

-- ============================================================
-- 3. RLS — same pattern as migrations 003 / 004
-- ============================================================
ALTER TABLE magic_link_tokens ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "Service role manages all magic tokens" ON magic_link_tokens;

CREATE POLICY "Service role manages all magic tokens"
    ON magic_link_tokens FOR ALL
    TO service_role
    USING (true)
    WITH CHECK (true);

-- ============================================================
-- 4. Tester setup (run these manually AFTER migration):
-- ============================================================
-- After the migration, mark your 2-3 tester friends in one go:
--
--   UPDATE users
--   SET is_tester = TRUE,
--       plan = 'tester',
--       email = COALESCE(email, 'friend@example.com'),
--       email_verified = TRUE
--   WHERE telegram_user_id IN (111111111, 222222222, 333333333);
--
-- Use real Telegram user IDs (numbers) — NOT usernames. To find a friend's
-- Telegram user_id: have them send /start to @userinfobot or @RawDataBot.

