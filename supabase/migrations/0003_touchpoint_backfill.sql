-- supabase/migrations/0003_touchpoint_backfill.sql
-- Importers don't just create contacts — they backfill dated touchpoints
-- (spec §3.5). Staged interactions carry match keys; `crm backfill` resolves
-- them to contacts after dedup.

create table staging_interactions (
  id uuid primary key default gen_random_uuid(),
  source text not null,
  source_external_id text not null,   -- importer-computed → idempotent re-import
  -- match keys (normalized by the importer; any one may identify the contact)
  email text,
  phone text,
  handle text,
  linkedin_url text,
  -- the touchpoint itself
  kind interaction_kind not null,
  channel text,
  occurred_at date,
  summary text,
  event_name text,                    -- when set, link to / create a shared event
  event_location text,
  -- resolution state
  match_status text not null default 'pending',  -- pending | linked | orphaned
  matched_contact_id uuid references contacts(id) on delete set null,
  imported_at timestamptz not null default now(),
  resolved_at timestamptz,
  unique (source, source_external_id)
);
create index staging_interactions_status on staging_interactions (match_status);

-- idempotent backfill target: interactions remember their staged origin
alter table interactions add column source text;
alter table interactions add column source_external_id text;
create unique index interactions_source_ext
  on interactions (source, source_external_id)
  where source_external_id is not null;

alter table staging_interactions enable row level security;
grant all on table staging_interactions to service_role;
