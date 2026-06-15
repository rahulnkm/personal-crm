-- supabase/migrations/0008_enrich_array.sql
-- Array-field write-path: set-union RPC + array reject helper.
--
-- Scalar attributes go through enrich_apply_candidate (survivorship — one current
-- winner). ARRAY fields (tags/affiliations/expertise/interests) have no single
-- winner; they accumulate. enrich_apply_array does an advisory-locked, idempotent,
-- tombstone-aware set-union with per-element provenance (is_current=false, since
-- there is no single golden value to elect).

create or replace function enrich_apply_array(
  p_contact_id uuid, p_field text, p_value text,
  p_method text, p_source text, p_confidence real,
  p_source_detail text default null, p_dry_run boolean default false)
returns text language plpgsql as $$
declare is_disputed boolean; already boolean; would text;
begin
  if p_field not in ('tags','affiliations','expertise','interests') then
    raise exception 'not an array field: %', p_field using errcode='22023';
  end if;
  if p_value is null or btrim(p_value)='' then return 'noop'; end if;
  perform pg_advisory_xact_lock(hashtext(p_contact_id::text || ':' || p_field));
  select exists(select 1 from enrichment_log where contact_id=p_contact_id and field=p_field
    and verification_status='disputed' and new_value is not distinct from p_value) into is_disputed;
  execute format('select $1 = any(coalesce(%I,''{}'')) from contacts where id=$2', p_field)
    into already using p_value, p_contact_id;
  if is_disputed then would:='tombstoned';
  elsif already then would:='already';
  else would:='added'; end if;
  if p_dry_run then return would; end if;
  if would <> 'added' then return would; end if;
  execute format('update contacts set %I = array_append(coalesce(%I,''{}''),$1), updated_at=now() where id=$2', p_field, p_field)
    using p_value, p_contact_id;
  insert into enrichment_log (contact_id, field, new_value, source, confidence, method, source_detail, is_current)
    values (p_contact_id, p_field, p_value, p_source, p_confidence, p_method, p_source_detail, false);
  return 'added';
end $$;

-- reject helper for arrays (mirrors the scalar reject/tombstone): write a disputed
-- provenance row for (field,value) so a later apply returns 'tombstoned', AND remove
-- the value from the array column so the golden record stops reflecting it.
create or replace function enrich_reject_array(
  p_contact_id uuid, p_field text, p_value text)
returns void language plpgsql as $$
begin
  if p_field not in ('tags','affiliations','expertise','interests') then
    raise exception 'not an array field: %', p_field using errcode='22023';
  end if;
  perform pg_advisory_xact_lock(hashtext(p_contact_id::text || ':' || p_field));
  insert into enrichment_log (contact_id, field, new_value, source, method, verification_status, is_current)
    values (p_contact_id, p_field, p_value, 'enrich_reject', 'enrich_reject', 'disputed', false);
  execute format('update contacts set %I = array_remove(coalesce(%I,''{}''), $1), updated_at=now() where id=$2', p_field, p_field)
    using p_value, p_contact_id;
end $$;
