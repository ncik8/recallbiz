-- Migration 010: T&C + Privacy Policy consent tracking
--
-- Tracks when each user accepted our terms. We block dashboard access until
-- both columns are non-null. Nullable so existing users aren't grandfathered
-- in silently -- they have to re-accept on next login.
--
-- accepted_via records WHERE they accepted (web dashboard vs telegram bot)
-- so we can prove consent origin if challenged.

ALTER TABLE public.users
    ADD COLUMN IF NOT EXISTS accepted_tos_at        TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS accepted_privacy_at    TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS accepted_via           TEXT;  -- 'web' | 'bot' | 'admin'

-- Index for the "who hasn't accepted yet?" query (should be empty after first login)
CREATE INDEX IF NOT EXISTS idx_users_consent_missing
    ON public.users (id)
    WHERE accepted_tos_at IS NULL OR accepted_privacy_at IS NULL;