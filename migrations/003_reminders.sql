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
-- 4. Row Level Security
-- ============================================================
ALTER TABLE reminders ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "Users see own reminders" ON reminders;
DROP POLICY IF EXISTS "Users insert own reminders" ON reminders;
DROP POLICY IF EXISTS "Users update own reminders" ON reminders;
DROP POLICY IF EXISTS "Users delete own reminders" ON reminders;

CREATE POLICY "Users see own reminders"
    ON reminders FOR SELECT
    USING (user_id = (SELECT id FROM users WHERE telegram_user_id = (
        (current_setting('request.jwt.claims', true)::json->>'telegram_user_id')::text
    )));

CREATE POLICY "Users insert own reminders"
    ON reminders FOR INSERT
    WITH CHECK (user_id = (SELECT id FROM users WHERE telegram_user_id = (
        (current_setting('request.jwt.claims', true)::json->>'telegram_user_id')::text
    )));

CREATE POLICY "Users update own reminders"
    ON reminders FOR UPDATE
    USING (user_id = (SELECT id FROM users WHERE telegram_user_id = (
        (current_setting('request.jwt.claims', true)::json->>'telegram_user_id')::text
    )));

CREATE POLICY "Users delete own reminders"
    ON reminders FOR DELETE
    USING (user_id = (SELECT id FROM users WHERE telegram_user_id = (
        (current_setting('request.jwt.claims', true)::json->>'telegram_user_id')::text
    )));

-- Note: the bot uses service_role key which bypasses RLS, so it can manage
-- reminders for any user. The app-layer still filters by user_id.

-- ============================================================
-- 5. touch_user updated_at trigger already covers users (001).
-- ============================================================
