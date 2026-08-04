begin;

drop index if exists public.supporters_public_order_idx;
drop index if exists public.supporters_public_ranking_idx;

create index supporters_public_order_idx
    on public.supporters (is_visible, amount desc, created_at desc, id desc);

notify pgrst, 'reload schema';

commit;
