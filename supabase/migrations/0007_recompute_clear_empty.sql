-- supabase/migrations/0007_recompute_clear_empty.sql
-- Patch backfill_recompute_contacts: contacts that have NO remaining
-- interactions (e.g. because their only interaction was re-pointed to another
-- contact via bulk_upsert_interactions) must have last_touchpoint_* nulled out.
-- Without this, a contact that loses its last interaction keeps stale denorm.

create or replace function backfill_recompute_contacts(
  contact_ids uuid[], channel_tier jsonb, tier_rank jsonb)
returns void
language plpgsql
set search_path = public, extensions
as $$
begin
  -- Step 0: clear touchpoint fields for contacts that no longer have ANY interaction.
  -- The join updates below only touch contacts that appear in interactions, so
  -- abandoned contacts (zero interactions after a re-point) would be missed.
  update contacts c
  set last_touchpoint_at      = null,
      last_touchpoint_channel = null,
      last_touchpoint_topic   = null,
      updated_at              = now()
  where c.id = any(contact_ids)
    and not exists (
      select 1 from interactions i where i.contact_id = c.id
    );

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
