-- 001_analytics_events.sql
-- Adds the analytics_events table for the TRCE funnel dashboard.
-- Distinct from public.events (which tracks trips like "Token2049").
--
-- Idempotent: safe to re-run.
-- Created: 2026-06-23

create table if not exists public.analytics_events (
    id uuid primary key default uuid_generate_v4(),
    user_id uuid references public.users(id) on delete set null,
    event_name text not null,
    properties jsonb,
    created_at timestamptz not null default now()
);

-- Lookups by event (hot path on /admin/funnel aggregation)
create index if not exists idx_analytics_events_name_created
    on public.analytics_events(event_name, created_at desc);

create index if not exists idx_analytics_events_user
    on public.analytics_events(user_id);

-- RLS: deny everything from anon/authenticated at the table level.
-- Only the service_role (used by the bot + admin dashboard) bypasses RLS.
alter table public.analytics_events enable row level security;

drop policy if exists "deny all on analytics_events" on public.analytics_events;
create policy "deny all on analytics_events"
    on public.analytics_events
    for all
    to public
    using (false)
    with check (false);