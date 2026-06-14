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
