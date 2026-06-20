-- Migration 008: Password authentication for web dashboard
-- Run in: Supabase Dashboard → SQL Editor → New query → Cmd+Enter
--
-- Why: bot signup currently only sets email_verified (magic link). To unlock the
-- web dashboard at trce.io/dashboard, users need a password they can type into a
-- login form. We hash with bcrypt (handled in Python via the bcrypt package) and
-- store only the hash here.
--
-- Flow:
--   1. Bot /signup → magic link confirms → user types password twice in DM
--   2. Bot bcrypts the password, stores users.password_hash
--   3. Web /login at trce.io reads email + password, bcrypt-verifies, sets session
--
-- Existing magic-link-only users: NULL password_hash. They use /setpassword in the
-- bot to upgrade. No backfill — a password is opt-in until they want the dashboard.

ALTER TABLE users
    ADD COLUMN IF NOT EXISTS password_hash TEXT,
    ADD COLUMN IF NOT EXISTS password_set_at TIMESTAMPTZ;

COMMENT ON COLUMN users.password_hash IS
    'Bcrypt hash of the user-chosen password (cost factor 12). NULL means magic-link-only — user has not yet opted into the web dashboard. Set via /setpassword bot command or post-signup password prompt.';

COMMENT ON COLUMN users.password_set_at IS
    'When password_hash was set. Useful for security audits (force rotate after N months).';

-- Index on email is critical for /login lookups. We already have UNIQUE on email
-- from migration 005, so it has an implicit btree index. No new index needed.

-- Down-migration (in case of rollback):
-- ALTER TABLE users DROP COLUMN IF EXISTS password_hash, DROP COLUMN IF EXISTS password_set_at;