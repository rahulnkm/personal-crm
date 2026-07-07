-- check_cloud_rpcs.sql — list migration-defined functions ABSENT from this database.
-- Run in the Supabase SQL editor (or: psql "$SUPABASE_DB_URL" -f scripts/check_cloud_rpcs.sql).
-- Reliable + read-only: queries the catalog directly (REST cannot detect presence safely —
-- PostgREST resolves overloads by argument-name set, so an empty POST false-flags every
-- multi-arg function as missing). Expected list generated from supabase/migrations/*.sql.
with expected(name) as (values
    ('backfill_recompute_contacts'),
    ('bulk_add_tag'),
    ('bulk_bump_last_touchpoint'),
    ('bulk_insert_identities'),
    ('bulk_upsert_interactions'),
    ('create_contacts_with_identities'),
    ('crm_stats'),
    ('enrich_apply_array'),
    ('enrich_apply_candidate'),
    ('enrich_recompute_field'),
    ('enrich_refresh_after'),
    ('enrich_reject_array'),
    ('enrich_seed_provenance'),
    ('match_contacts_by_name'),
    ('match_contacts_by_names')
)
select e.name as missing_function
from expected e
left join pg_proc p
  on p.proname = e.name
 and p.pronamespace = 'public'::regnamespace
where p.proname is null
order by e.name;
-- Zero rows = all defined functions are live. Rows = redeploy the migration(s) defining them.
