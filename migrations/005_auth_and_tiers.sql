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
