-- Add structured, privacy-bounded website visit analytics.
-- Safe to run more than once in the Supabase SQL Editor.

alter table public.visit_events
    add column if not exists analytics jsonb not null default '{}'::jsonb;

grant select, insert, update on table public.visit_events to service_role;

notify pgrst, 'reload schema';
