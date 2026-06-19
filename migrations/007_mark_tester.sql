-- Migration 007: Mark a user as tester
--
-- Testers bypass the signup gate AND the 10-contact free-tier cap.
-- Run this after getting the tester's Telegram user_id (they'll see it
-- in the bot logs when they /start the bot).
--
-- Usage:
--   UPDATE users
--   SET is_tester = TRUE, plan = 'tester', email_verified = TRUE
--   WHERE telegram_user_id = 123456789;
--
-- Then verify:
--   SELECT telegram_user_id, username, is_tester, plan, email_verified
--   FROM users WHERE is_tester = TRUE;
--
-- Find a user's telegram_user_id if you don't know it:
--   SELECT telegram_user_id, username, created_at
--   FROM users ORDER BY created_at DESC LIMIT 10;

-- Idempotent: safe to re-run.
UPDATE users
SET is_tester = TRUE,
    plan = 'tester',
    email_verified = TRUE,
    updated_at = NOW()
WHERE telegram_user_id IN (
    -- Add tester IDs here, comma-separated, e.g. 111111111, 222222222
    0  -- placeholder, no-op until you fill in real IDs
);

-- After running, uncomment to verify which users are now testers:
-- SELECT telegram_user_id, username, is_tester, plan, email_verified, created_at
-- FROM users
-- WHERE is_tester = TRUE
-- ORDER BY created_at DESC;
