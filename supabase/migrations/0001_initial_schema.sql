create extension if not exists pg_trgm with schema extensions;

-- enums
create type connection_status as enum ('in_network','contact_on_file');
create type closeness_tier as enum ('t1_irl_messaging','t2_dm','t3_community','t4_public','none');
create type email_status as enum ('verified','risky','invalid','unknown');
create type interaction_kind as enum ('origin','event','email','message','call','meeting');
create type match_status as enum ('pending','auto_matched','needs_review','merged','rejected');

-- registry of writers (Rahul + every agent/importer)
create table agents (
  id text primary key,
  description text not null,
  first_seen timestamptz not null default now(),
  last_active timestamptz not null default now()
);

create table tag_registry (
  tag text primary key,
  description text not null,
  created_by text not null references agents(id),
  created_at timestamptz not null default now()
);

-- golden record: one row per unique human
create table contacts (
  id uuid primary key default gen_random_uuid(),
  full_name text not null,
  first_name text,
  last_name text,
  "current_role" text,
  current_company text,
  location text,
  connection_status connection_status not null default 'contact_on_file',
  closeness_tier closeness_tier not null default 'none',
  affiliations text[] not null default '{}',
  origin_context text,
  notes text,
  tags text[] not null default '{}',
  email_status email_status not null default 'unknown',
  last_touchpoint_at date,
  last_touchpoint_channel text,
  last_touchpoint_topic text,
  last_enriched_at date,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
create index contacts_name_trgm on contacts using gin (lower(full_name) extensions.gin_trgm_ops);
create index contacts_tags_gin on contacts using gin (tags);
create index contacts_affiliations_gin on contacts using gin (affiliations);

-- XREF layer: immutable source identities pointing at golden records
create table contact_identities (
  id uuid primary key default gen_random_uuid(),
  contact_id uuid not null references contacts(id) on delete cascade,
  source text not null,
  source_external_id text,
  email text,
  phone text,
  linkedin_url text,
  handle text,
  raw_json jsonb,
  imported_at timestamptz not null default now()
);
create index identities_contact on contact_identities (contact_id);
create index identities_email on contact_identities (email) where email is not null;
create index identities_phone on contact_identities (phone) where phone is not null;
create index identities_linkedin on contact_identities (linkedin_url) where linkedin_url is not null;
create unique index identities_source_external_id
  on contact_identities (source, source_external_id)
  where source_external_id is not null;

-- staging: raw imports awaiting entity resolution
create table staging (
  id uuid primary key default gen_random_uuid(),
  source text not null,
  source_external_id text not null,  -- importer-computed row hash → idempotent re-import
  full_name text,
  email text,
  phone text,
  linkedin_url text,
  handle text,
  role text,
  company text,
  location text,
  raw_json jsonb,
  match_status match_status not null default 'pending',
  -- on delete set null: crm merge deletes the dropped contact; staging rows
  -- pointing at it must not block the delete
  matched_contact_id uuid references contacts(id) on delete set null,
  match_confidence real,
  match_method text,
  imported_at timestamptz not null default now(),
  resolved_at timestamptz,
  unique (source, source_external_id)
);
create index staging_status on staging (match_status);

-- shared occasions (group touchpoints)
create table events (
  id uuid primary key default gen_random_uuid(),
  name text not null,
  occurred_at date,
  location text,
  event_notes text,
  source text,
  created_by text references agents(id),
  created_at timestamptz not null default now()
);

-- dated touchpoints; facts append-only, summary editable
create table interactions (
  id uuid primary key default gen_random_uuid(),
  contact_id uuid not null references contacts(id) on delete cascade,
  event_id uuid references events(id),
  kind interaction_kind not null,
  channel text,
  occurred_at date,
  summary text,
  logged_by text not null references agents(id),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
create index interactions_contact on interactions (contact_id);
create index interactions_event on interactions (event_id) where event_id is not null;

-- per-field provenance + job-change history
create table enrichment_log (
  id uuid primary key default gen_random_uuid(),
  contact_id uuid not null references contacts(id) on delete cascade,
  field text not null,
  old_value text,
  new_value text,
  source text not null,
  confidence real,
  method text,
  created_at timestamptz not null default now()
);
create index enrichment_contact on enrichment_log (contact_id);

-- fuzzy candidate lookup (PostgREST can't express similarity(); expose as RPC)
create or replace function match_contacts_by_name(q text, lim int default 5)
returns table(contact_id uuid, full_name text, score real)
language sql stable
set search_path = public, extensions
as $$
  select c.id, c.full_name,
         extensions.similarity(lower(c.full_name), lower(q))::real as score
  from contacts c
  where lower(c.full_name) % lower(q)
  order by score desc
  limit lim
$$;

-- security: deny-all RLS; only the secret key (bypasses RLS) gets in
alter table agents enable row level security;
alter table tag_registry enable row level security;
alter table contacts enable row level security;
alter table contact_identities enable row level security;
alter table staging enable row level security;
alter table events enable row level security;
alter table interactions enable row level security;
alter table enrichment_log enable row level security;

-- post-May-2026 default-deny Data API: service_role needs explicit grants
grant usage on schema public to service_role;
grant all on all tables in schema public to service_role;
grant execute on all functions in schema public to service_role;

-- seed: Rahul is the default writing agent
insert into agents (id, description) values ('rahul', 'Rahul himself — manual CLI use');
