-- supabase/migrations/0005_dedup_clustering.sql
-- Parallel dedup support (spec: 2026-06-13-dedup-batching-design.md).
-- NO new match_status enum values — claim coordination is in-memory (single
-- invocation). dedup_cluster is for observability/debugging of plan grouping.

alter table staging add column dedup_cluster text;

-- bulk fuzzy: best EXISTING contact per input name, keyed to input ordinality.
-- Mirrors match_contacts_by_name (0002) but array-in. Faithful best-of-1.
create or replace function match_contacts_by_names(names text[], lim int default 1)
returns table(idx int, contact_id uuid, full_name text, score real)
language sql stable
set search_path = public, extensions
-- NB: deliberately NO `set pg_trgm.similarity_threshold` — the cloud role lacks
-- permission to pin that GUC at function level, and it's unnecessary here: the
-- `%` prefilter's session default (0.3) sits BELOW the explicit `score >= 0.55`
-- floor below, so the floor governs which matches return. Correctness identical.
as $$
  select n.idx::int, m.id, m.full_name, m.score
  from unnest(names) with ordinality as n(q, idx)
  cross join lateral (
    select c.id, c.full_name,
           extensions.similarity(f_unaccent(lower(c.full_name)),
                                  f_unaccent(lower(n.q)))::real as score
    from contacts c
    where f_unaccent(lower(c.full_name)) % f_unaccent(lower(n.q))
    order by score desc
    limit 1
  ) m
  where m.score >= 0.55;   -- REVIEW_BAND floor; below = no match
$$;
grant execute on function match_contacts_by_names(text[], int) to service_role;

-- atomic contact + anchor identity creation, batched. plpgsql body = one
-- transaction → no orphan-contact window ever. payload: jsonb array of
-- {create_key, contact:{...}, identity:{...}}.
create or replace function create_contacts_with_identities(payload jsonb)
returns table(create_key text, contact_id uuid)
language plpgsql
set search_path = public, extensions
as $$
declare
  item jsonb;
  new_id uuid;
begin
  for item in select * from jsonb_array_elements(payload)
  loop
    insert into contacts (full_name, "current_role", current_company, location)
    values (item->'contact'->>'full_name',
            item->'contact'->>'current_role',
            item->'contact'->>'current_company',
            item->'contact'->>'location')
    returning id into new_id;

    insert into contact_identities
      (contact_id, source, source_external_id, email, phone, linkedin_url, handle, raw_json)
    values (new_id,
            item->'identity'->>'source',
            item->'identity'->>'source_external_id',
            item->'identity'->>'email',
            item->'identity'->>'phone',
            item->'identity'->>'linkedin_url',
            item->'identity'->>'handle',
            item->'identity'->'raw_json');

    create_key := item->>'create_key';
    contact_id := new_id;
    return next;
  end loop;
end
$$;
grant execute on function create_contacts_with_identities(jsonb) to service_role;
