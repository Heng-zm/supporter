-- Fix Telegram /add idempotency for PostgREST/Supabase.
-- Safe to run more than once in the Supabase SQL Editor.

alter table public.supporters
    add column if not exists telegram_update_id bigint;

drop index if exists public.supporters_telegram_update_id_idx;
create unique index supporters_telegram_update_id_idx
    on public.supporters (telegram_update_id);

grant select, insert, update, delete on table public.supporters to service_role;

notify pgrst, 'reload schema';
