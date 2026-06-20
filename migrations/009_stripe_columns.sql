-- Migration 009: Stripe billing columns on users
-- Adds subscription tracking so webhook can flip plan='pro' on payment.
-- All columns nullable — existing rows untouched, default plan='free' still applies.

ALTER TABLE public.users
    ADD COLUMN IF NOT EXISTS stripe_customer_id       TEXT,
    ADD COLUMN IF NOT EXISTS stripe_subscription_id  TEXT,
    ADD COLUMN IF NOT EXISTS subscription_status      TEXT,    -- 'active' | 'trialing' | 'past_due' | 'canceled' | 'incomplete'
    ADD COLUMN IF NOT EXISTS subscription_period_end TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS pro_since                TIMESTAMPTZ;

-- Indexes for webhook lookups. The webhook identifies users by stripe_customer_id
-- and stripe_subscription_id; both must be indexable.
CREATE UNIQUE INDEX IF NOT EXISTS idx_users_stripe_customer_id
    ON public.users (stripe_customer_id)
    WHERE stripe_customer_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_users_stripe_subscription_id
    ON public.users (stripe_subscription_id)
    WHERE stripe_subscription_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_users_plan
    ON public.users (plan)
    WHERE plan IN ('pro', 'team');