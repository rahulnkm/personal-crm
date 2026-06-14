-- supabase/migrations/0006_enrichment_substrate.sql
-- enrichment_log: promote from conflict-log to provenance spine
do $$ begin
  create type enrich_verification as enum ('unverified','verified','disputed','human_confirmed');
exception when duplicate_object then null; end $$;

alter table enrichment_log
  add column if not exists source_detail text,
  add column if not exists verification_status enrich_verification not null default 'unverified',
  add column if not exists refresh_after date,
  add column if not exists is_current boolean not null default false;

-- exactly one current scalar winner per (contact, field)
create unique index if not exists enrichment_log_current_uq
  on enrichment_log (contact_id, field) where is_current;
-- ranking + lookup support
create index if not exists enrichment_log_field_rank
  on enrichment_log (contact_id, field, created_at desc);

-- contacts: capability + social columns (Plan 1 set only)
alter table contacts
  add column if not exists company_category text,
  add column if not exists company_description text,
  add column if not exists company_domain text,
  add column if not exists expertise text[] not null default '{}',
  add column if not exists interests text[] not null default '{}',
  add column if not exists avatar_url text,
  add column if not exists github_username text,
  add column if not exists twitter_username text,
  add column if not exists website_url text;

-- human-in-the-loop review queue
create table if not exists enrich_review (
  id uuid primary key default gen_random_uuid(),
  contact_id uuid not null references contacts(id) on delete cascade,
  field text not null,
  candidate_value text,
  source text not null,
  confidence real,
  reason text,                              -- low_confidence | value_conflict | identifier_conflict
  other_contact_id uuid references contacts(id) on delete set null,
  status text not null default 'open',      -- open | resolved | skipped
  created_at timestamptz not null default now(),
  resolved_at timestamptz
);
create index if not exists enrich_review_open on enrich_review (status, created_at);

-- quarantined discovered identifiers (not live match keys until promoted)
create table if not exists candidate_identities (
  id uuid primary key default gen_random_uuid(),
  contact_id uuid not null references contacts(id) on delete cascade,
  kind text not null,                       -- email | linkedin_url | phone | handle
  value text not null,
  source text not null,
  confidence real,
  source_detail text,
  status text not null default 'pending',   -- pending | promoted | rejected
  created_at timestamptz not null default now(),
  unique (contact_id, kind, value)
);

-- helper: recompute + materialize the winner for one scalar (contact, field)
create or replace function enrich_recompute_field(p_contact_id uuid, p_field text)
returns text language plpgsql as $$
declare win record; col_type text; begin
  -- the golden column may be a non-text type (e.g. the connection_status enum);
  -- provenance stores values as text, so materialization casts back to the
  -- column's declared type (text->enum, text->date, text->text no-op).
  select udt_name into col_type from information_schema.columns
   where table_schema = 'public' and table_name = 'contacts' and column_name = p_field;
  select id, new_value into win
  from enrichment_log e
  where e.contact_id = p_contact_id and e.field = p_field
    and e.verification_status is distinct from 'disputed'
    -- exclude any value that has a disputed tombstone for this (contact, field)
    and not exists (
      select 1 from enrichment_log d
      where d.contact_id = p_contact_id and d.field = p_field
        and d.verification_status = 'disputed'
        and d.new_value is not distinct from e.new_value)
  order by (case when e.method = 'manual_set' then 0 else 1 end) asc,
           e.created_at desc,
           coalesce(e.confidence, 0.4) desc
  limit 1;

  -- demote all, elect winner
  update enrichment_log set is_current = false
   where contact_id = p_contact_id and field = p_field and is_current;
  if win.id is not null then
    update enrichment_log set is_current = true where id = win.id;
    execute format('update contacts set %I = $1::text::%I, updated_at = now() where id = $2',
                   p_field, col_type)
      using win.new_value, p_contact_id;
  else
    -- no surviving candidate (e.g. sole value was tombstoned) → clear the golden column
    execute format('update contacts set %I = null, updated_at = now() where id = $1', p_field)
      using p_contact_id;
  end if;
  return win.new_value;  -- NULL when no winner
end $$;

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
    return 'noop';  -- arrays handled by set-union path, not survivorship
  end if;

  perform pg_advisory_xact_lock(hashtext(p_contact_id::text || ':' || p_field));

  -- rejected value can never win again
  select exists(select 1 from enrichment_log where contact_id=p_contact_id and field=p_field
    and verification_status='disputed' and new_value is not distinct from p_value) into is_disputed;

  -- is there a manual value already?
  select exists(select 1 from enrichment_log where contact_id=p_contact_id and field=p_field
    and method='manual_set' and is_current) into manual_exists;

  -- compute would-be outcome
  if p_method = 'manual_set' then would := 'golden';
  elsif is_disputed then would := 'losing';
  elsif manual_exists then would := 'review';
  elsif coalesce(p_confidence,0) >= accept_threshold then would := 'golden';
  else would := 'review';
  end if;

  if p_dry_run then return would; end if;

  -- disputed/tombstoned value: short-circuit BEFORE idempotency + insert, so a
  -- re-applied rejected value deterministically returns 'losing' (not 'noop').
  if would = 'losing' then return 'losing'; end if;

  -- idempotency: skip if an identical row already exists (EXISTS ignores ordering)
  if exists (
    select 1 from enrichment_log
    where contact_id=p_contact_id and field=p_field
      and new_value is not distinct from p_value and source = p_source) then
    return 'noop';
  end if;

  insert into enrichment_log (contact_id, field, old_value, new_value, source, confidence, method, source_detail)
  select p_contact_id, p_field,
         (select new_value from enrichment_log where contact_id=p_contact_id and field=p_field and is_current limit 1),
         p_value, p_source, p_confidence, p_method, p_source_detail;

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
