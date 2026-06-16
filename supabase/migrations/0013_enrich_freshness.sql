-- Freshness clock: per-field TTL helper + stamp refresh_after on scalar provenance.
-- Manual edits never expire; volatile fields age (90/180d); stable fields never.
-- (Renumbered above main's 0009 for the merge; see the design spec.)

create or replace function enrich_refresh_after(p_field text, p_method text)
returns date language sql immutable as $$
  select case
    when p_method = 'manual_set' then null
    when p_field in ('current_role','current_company','company_category') then current_date + 90
    when p_field in ('location','email_status') then current_date + 180
    else null
  end;
$$;

-- enrich_apply_candidate, identical to 0006 except the provenance INSERT now stamps
-- refresh_after := enrich_refresh_after(p_field, p_method).
create or replace function enrich_apply_candidate(
  p_contact_id uuid, p_field text, p_value text,
  p_method text, p_source text, p_confidence real,
  p_source_detail text default null, p_dry_run boolean default false)
returns text language plpgsql as $$
declare
  accept_threshold real := 0.7;
  manual_exists boolean;
  is_disputed boolean;
  would text;
begin
  if p_field in ('tags','affiliations','expertise','interests') then
    return 'noop';
  end if;

  if not exists (select 1 from information_schema.columns
      where table_schema='public' and table_name='contacts' and column_name = p_field) then
    raise exception 'unknown contacts field: %', p_field using errcode = '22023';
  end if;

  perform pg_advisory_xact_lock(hashtext(p_contact_id::text || ':' || p_field));

  select exists(select 1 from enrichment_log where contact_id=p_contact_id and field=p_field
    and verification_status='disputed' and new_value is not distinct from p_value) into is_disputed;

  select exists(select 1 from enrichment_log where contact_id=p_contact_id and field=p_field
    and method='manual_set' and is_current) into manual_exists;

  if p_method = 'manual_set' then would := 'golden';
  elsif is_disputed then would := 'losing';
  elsif manual_exists then would := 'review';
  elsif coalesce(p_confidence,0) >= accept_threshold then would := 'golden';
  else would := 'review';
  end if;

  if p_dry_run then return would; end if;
  if would = 'losing' then return 'losing'; end if;

  if exists (
    select 1 from enrichment_log
    where contact_id=p_contact_id and field=p_field
      and new_value is not distinct from p_value and source = p_source) then
    return 'noop';
  end if;

  insert into enrichment_log (contact_id, field, old_value, new_value, source, confidence, method, source_detail, refresh_after)
  select p_contact_id, p_field,
         (select new_value from enrichment_log where contact_id=p_contact_id and field=p_field and is_current limit 1),
         p_value, p_source, p_confidence, p_method, p_source_detail, enrich_refresh_after(p_field, p_method);

  if would = 'review' then
    insert into enrich_review (contact_id, field, candidate_value, source, confidence, reason)
    values (p_contact_id, p_field, p_value, p_source, p_confidence,
            case when manual_exists then 'value_conflict' else 'low_confidence' end);
    return 'review';
  end if;

  if would = 'golden' then
    perform enrich_recompute_field(p_contact_id, p_field);
    return 'golden';
  end if;
  return 'losing';
end $$;
