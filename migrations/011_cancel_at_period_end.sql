-- Migration 011: Add cancel_at_period_end column to users
--
-- Why: when a user cancels via the Stripe billing portal, Stripe keeps
-- status='active' until the period ends (cancel_at_period_end=True). My
-- webhook handler was treating that as still-pro, so the dashboard showed
-- 'Pro' with no indication of cancellation.
--
-- Now: store the boolean. Dashboard reads it and shows a banner
-- "Pro ends [date]" when set. At period end, Stripe fires
-- customer.subscription.deleted and we flip plan='free' + status='canceled'.

ALTER TABLE users
  ADD COLUMN IF NOT EXISTS cancel_at_period_end BOOLEAN DEFAULT FALSE;

-- No index needed — we filter by user id (primary key lookup).

-- Backfill: existing users with status='active' who haven't had this column
-- set will get NULL (default FALSE). The next subscription.updated webhook
-- will set it correctly. No need to backfill from Stripe now.