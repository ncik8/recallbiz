-- Migration 012: password_reset_tokens table
--
-- Why: users sign up via the Telegram bot which sets their password. If they
-- forget it (or want to change it from another device), they currently have
-- no way to recover — no /forgot flow on trce.io.
--
-- This table stores one-time-use tokens for the password reset flow:
--   1. User submits email at /forgot
--   2. Server generates a token, stores with 1h expiry, sends reset email
--   3. User clicks email link → /reset/<token>
--   4. Server validates (token exists, not expired, not used), accepts new password
--   5. Mark token used, hash new password, log user in
--
-- Tokens are stored as raw strings. The token IS the secret (32 bytes urlsafe).
-- It's only valid for 1 hour and one-time use. Don't log it.

CREATE TABLE IF NOT EXISTS password_reset_tokens (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  token TEXT NOT NULL UNIQUE,
  expires_at TIMESTAMPTZ NOT NULL,
  used_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Fast lookup by token (the unique index already covers this).
-- Also useful for cron cleanup of expired tokens.
CREATE INDEX IF NOT EXISTS idx_password_reset_tokens_expires_at
  ON password_reset_tokens(expires_at);

-- RLS: only service_role can read/write. Web app uses service_role key.
ALTER TABLE password_reset_tokens ENABLE ROW LEVEL SECURITY;

-- Service-role full access (bot + web both use service_role key).
DROP POLICY IF EXISTS "service_role full access on password_reset_tokens" ON password_reset_tokens;
CREATE POLICY "service_role full access on password_reset_tokens"
  ON password_reset_tokens FOR ALL TO service_role USING (true) WITH CHECK (true);