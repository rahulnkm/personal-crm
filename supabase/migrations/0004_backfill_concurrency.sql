-- supabase/migrations/0004_backfill_concurrency.sql
-- Parallel backfill support (spec: 2026-06-13-backfill-batching-design.md).
-- 1) Contact denormalization moves server-side: workers never write contacts;
--    this function recomputes last_touchpoint_* and closeness_tier from
--    interactions (ground truth) for the touched contacts, in one call.
--    The channel→tier and tier→rank mappings are PASSED IN as jsonb so
--    src/crm/closeness.py stays the single source of truth (no SQL copy).
-- 2) Events get a uniqueness guard so two workers cannot create the same
--    backfill event (NULL occurred_at folded via coalesce — Postgres treats
--    NULLs as distinct in unique indexes).

create or replace function backfill_recompute_contacts(
  contact_ids uuid[], channel_tier jsonb, tier_rank jsonb)
returns void
language plpgsql
set search_path = public, extensions
as $$
begin
  -- last touchpoint <- latest DATED interaction (NULLs never win); ties by created_at
  update contacts c
  set last_touchpoint_at      = l.occurred_at,
      last_touchpoint_channel = l.channel,
      last_touchpoint_topic   = l.summary,
      updated_at              = now()
  from (
    select distinct on (contact_id) contact_id, occurred_at, channel, summary
    from interactions
    where contact_id = any(contact_ids) and occurred_at is not null
    order by contact_id, occurred_at desc, created_at desc
  ) l
  where c.id = l.contact_id;

  -- tier <- highest rank among current tier and all channel evidence (never downgrades)
  update contacts c
  set closeness_tier = bt.tier_name::closeness_tier,
      updated_at     = now()
  from (
    select x.contact_id,
           x.best_rank,
           (select je.key from jsonb_each_text(tier_rank) je
            where je.value::int = x.best_rank limit 1) as tier_name
    from (
      select i.contact_id,
             max(coalesce((tier_rank ->> (channel_tier ->> i.channel))::int, 0)) as best_rank
      from interactions i
      where i.contact_id = any(contact_ids)
      group by i.contact_id
    ) x
  ) bt
  where c.id = bt.contact_id
    and bt.tier_name is not null
    and bt.best_rank > coalesce((tier_rank ->> (c.closeness_tier::text))::int, 0);
end
$$;

grant execute on function backfill_recompute_contacts(uuid[], jsonb, jsonb) to service_role;

create unique index events_backfill_unique
  on events (name, coalesce(occurred_at, '0001-01-01'::date))
  where source = 'backfill';
