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
