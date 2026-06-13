-- Accent-insensitive fuzzy matching: 'Jose Garcia' must find 'José García'.
-- pg_trgm treats é and e as different chars, killing trigram overlap; unaccent
-- folds both sides before comparison.
create extension if not exists unaccent with schema extensions;

-- unaccent() is only stable (dictionary-dependent); an index needs immutable
create or replace function public.f_unaccent(text) returns text
language sql immutable strict parallel safe
set search_path = public, extensions
as $$ select extensions.unaccent($1) $$;

drop index if exists contacts_name_trgm;
create index contacts_name_trgm on contacts
  using gin (f_unaccent(lower(full_name)) extensions.gin_trgm_ops);

-- pin the % prefilter threshold too: if anyone raises the GUC above REVIEW_BAND
-- the engine would silently under-return and fragment
create or replace function match_contacts_by_name(q text, lim int default 5)
returns table(contact_id uuid, full_name text, score real)
language sql stable
set search_path = public, extensions
set pg_trgm.similarity_threshold = 0.3
as $$
  select c.id, c.full_name,
         extensions.similarity(f_unaccent(lower(c.full_name)), f_unaccent(lower(q)))::real as score
  from contacts c
  where f_unaccent(lower(c.full_name)) % f_unaccent(lower(q))
  order by score desc
  limit lim
$$;

grant execute on all functions in schema public to service_role;
