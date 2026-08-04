create extension if not exists pgcrypto;

create table if not exists public.supporters (
    id uuid primary key default gen_random_uuid(),
    name text not null check (char_length(name) between 1 and 80),
    amount numeric(14, 2) not null check (amount > 0 and amount <= 1000000000),
    currency varchar(3) not null default 'USD',
    message varchar(280),
    avatar_url varchar(1000),
    payment_method varchar(40),
    is_visible boolean not null default true,
    telegram_update_id bigint,
    created_at timestamptz not null default now()
);

alter table public.supporters add column if not exists currency varchar(3) not null default 'USD';
alter table public.supporters add column if not exists message varchar(280);
alter table public.supporters add column if not exists avatar_url varchar(1000);
alter table public.supporters add column if not exists payment_method varchar(40);
alter table public.supporters add column if not exists is_visible boolean not null default true;
alter table public.supporters add column if not exists telegram_update_id bigint;
alter table public.supporters add column if not exists created_at timestamptz not null default now();

-- PostgreSQL unique indexes allow multiple NULL values. This must be a normal
-- (non-partial) unique index so PostgREST can use on_conflict=telegram_update_id.
drop index if exists public.supporters_telegram_update_id_idx;
create unique index supporters_telegram_update_id_idx
    on public.supporters (telegram_update_id);
create index if not exists supporters_public_order_idx
    on public.supporters (is_visible, amount desc, created_at desc, id desc);

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
    user_agent text not null,
    ip_hash text not null,
    ip_masked text,
    country text,
    region text,
    city text,
    telegram_sent boolean not null default false,
    telegram_message_id text,
    telegram_error varchar(500),
    created_at timestamptz not null default now()
);

alter table public.visit_events add column if not exists telegram_sent boolean not null default false;
alter table public.visit_events add column if not exists telegram_message_id text;
alter table public.visit_events add column if not exists telegram_error varchar(500);
alter table public.visit_events add column if not exists created_at timestamptz not null default now();
alter table public.visit_events add column if not exists analytics jsonb not null default '{}'::jsonb;

create unique index if not exists visit_events_dedupe_key_idx
    on public.visit_events (dedupe_key);
create index if not exists visit_events_recent_visitor_idx
    on public.visit_events (ip_hash, created_at desc);

alter table public.supporters enable row level security;
alter table public.visit_events enable row level security;

-- The backend must use the Supabase service-role key. Do not expose that key to browsers.

-- Refresh PostgREST's schema cache after running this file in Supabase.
notify pgrst, 'reload schema';
