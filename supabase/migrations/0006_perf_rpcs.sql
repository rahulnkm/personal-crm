-- bulk_upsert_interactions: one statement insert-or-refresh against the partial
-- unique index interactions_source_ext (PostgREST .upsert() cannot target it).
-- Returns the PRIOR contact_ids of rows whose contact_id MOVED, so the caller can
-- recompute the abandoned contact's denorm.
create or replace function bulk_upsert_interactions(payload jsonb)
returns setof uuid
language sql
set search_path = public, extensions
as $$
  with prior as (
    select i.source_external_id, i.contact_id as old_cid
    from interactions i
    where i.source_external_id in (
      select p.source_external_id from jsonb_to_recordset(payload)
        as p(source_external_id text))
      and i.source_external_id is not null
  ),
  up as (
    insert into interactions
      (contact_id, event_id, kind, channel, occurred_at, summary,
       logged_by, source, source_external_id)
    select p.contact_id, p.event_id, p.kind, p.channel, p.occurred_at, p.summary,
           p.logged_by, p.source, p.source_external_id
    from jsonb_to_recordset(payload) as p(
      contact_id uuid, event_id uuid, kind interaction_kind, channel text,
      occurred_at date, summary text, logged_by text, source text,
      source_external_id text)
    on conflict (source, source_external_id) where source_external_id is not null
    do update set occurred_at = excluded.occurred_at, summary = excluded.summary,
                  event_id = excluded.event_id, contact_id = excluded.contact_id,
                  updated_at = now()
    returning source_external_id, contact_id as new_cid
  )
  select distinct prior.old_cid
  from up join prior using (source_external_id)
  where prior.old_cid is distinct from up.new_cid;
$$;
revoke execute on function bulk_upsert_interactions(jsonb) from public;
grant execute on function bulk_upsert_interactions(jsonb) to service_role;

-- bulk_bump_last_touchpoint: server-side guarded monotonic bump. Equal date = no-op.
create or replace function bulk_bump_last_touchpoint(
  p_ids uuid[], p_occurred date, p_channel text, p_topic text)
returns void
language sql
set search_path = public, extensions
as $$
  update contacts
  set last_touchpoint_at = p_occurred, last_touchpoint_channel = p_channel,
      last_touchpoint_topic = p_topic, updated_at = now()
  where id = any(p_ids)
    and (last_touchpoint_at is null or last_touchpoint_at < p_occurred);
$$;
revoke execute on function bulk_bump_last_touchpoint(uuid[], date, text, text) from public;
grant execute on function bulk_bump_last_touchpoint(uuid[], date, text, text) to service_role;

-- crm_stats: all coverage buckets in one round-trip. int casts so JSON renders 3 not 3.0.
create or replace function crm_stats()
returns jsonb
language sql
stable
set search_path = public, extensions
as $$
  select jsonb_build_object(
    'connection_status', (select coalesce(jsonb_object_agg(connection_status, c), '{}'::jsonb)
       from (select connection_status, count(*)::int c from contacts group by 1) s),
    'closeness_tier', (select coalesce(jsonb_object_agg(closeness_tier, c), '{}'::jsonb)
       from (select closeness_tier, count(*)::int c from contacts group by 1) s),
    'staging', (select coalesce(jsonb_object_agg(match_status, c), '{}'::jsonb)
       from (select match_status, count(*)::int c from staging group by 1) s),
    'touchpoints', (select coalesce(jsonb_object_agg(match_status, c), '{}'::jsonb)
       from (select match_status, count(*)::int c from staging_interactions group by 1) s),
    'contacts_total', (select count(*)::int from contacts)
  );
$$;
revoke execute on function crm_stats() from public;
grant execute on function crm_stats() to service_role;
-- ROLLBACK: drop function if exists bulk_upsert_interactions(jsonb);
--           drop function if exists bulk_bump_last_touchpoint(uuid[], date, text, text);
--           drop function if exists crm_stats();
