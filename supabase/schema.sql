-- Run once in Supabase SQL Editor.
-- The browser never receives a Supabase secret key.

create extension if not exists pgcrypto;

create table if not exists public.supporters (
  id uuid primary key default gen_random_uuid(),
  name text not null check (char_length(name) between 1 and 80),
  amount numeric(14, 2) not null check (amount > 0),
  currency text not null default 'USD' check (currency ~ '^[A-Z]{3}$'),
  message text check (message is null or char_length(message) <= 280),
  avatar_url text,
  payment_method text check (payment_method is null or char_length(payment_method) <= 40),
  is_visible boolean not null default true,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index if not exists supporters_public_ranking_idx
  on public.supporters (is_visible, amount desc, created_at desc);

create table if not exists public.visit_events (
  id uuid primary key default gen_random_uuid(),
  event_id text not null,
  dedupe_key text not null unique,
  client_timestamp text,
  url text,
  path text,
  referrer text,
  title text,
  device text,
  browser text,
  platform text,
  language text,
  timezone text,
  screen jsonb not null default '{}'::jsonb,
  connection jsonb not null default '{}'::jsonb,
  analytics jsonb not null default '{}'::jsonb,
  user_agent text,
  ip_hash text,
  ip_masked text,
  country text,
  region text,
  city text,
  telegram_sent boolean not null default false,
  telegram_message_id text,
  telegram_error text,
  created_at timestamptz not null default now()
);

create index if not exists visit_events_created_at_idx
  on public.visit_events (created_at desc);

create or replace function public.set_updated_at()
returns trigger
language plpgsql
security invoker
set search_path = public
as $$
begin
  new.updated_at = now();
  return new;
end;
$$;

drop trigger if exists supporters_set_updated_at on public.supporters;
create trigger supporters_set_updated_at
before update on public.supporters
for each row execute function public.set_updated_at();

alter table public.supporters enable row level security;
alter table public.visit_events enable row level security;

revoke all on table public.supporters from anon, authenticated;
revoke all on table public.visit_events from anon, authenticated;
grant all on table public.supporters to service_role;
grant all on table public.visit_events to service_role;
