-- bulk_insert_identities: batched insert-or-ignore against the PARTIAL unique index
-- identities_source_external_id (source, source_external_id) WHERE source_external_id
-- IS NOT NULL. PostgREST's .upsert(on_conflict=...) cannot target a partial unique
-- index (errors 42P10), so the dedup fold's batched identity write goes through this
-- RPC instead — same pattern as bulk_upsert_interactions in 0006. ON CONFLICT DO
-- NOTHING means a pre-existing or duplicate identity can never abort the batch
-- (correctness rule for the dedup auto_matched fold).
create or replace function bulk_insert_identities(payload jsonb)
returns void
language sql
set search_path = public, extensions
as $$
  insert into contact_identities
    (contact_id, source, source_external_id, email, phone, linkedin_url, handle, raw_json)
  select p.contact_id, p.source, p.source_external_id, p.email, p.phone,
         p.linkedin_url, p.handle, p.raw_json
  from jsonb_to_recordset(payload) as p(
    contact_id uuid, source text, source_external_id text, email text, phone text,
    linkedin_url text, handle text, raw_json jsonb)
  on conflict (source, source_external_id) where source_external_id is not null
  do nothing;
$$;
revoke execute on function bulk_insert_identities(jsonb) from public;
grant execute on function bulk_insert_identities(jsonb) to service_role;
-- ROLLBACK: drop function if exists bulk_insert_identities(jsonb);
