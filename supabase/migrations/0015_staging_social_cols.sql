-- supabase/migrations/0015_staging_social_cols.sql
-- CSV importers could see twitter/github/website columns but staging had
-- nowhere to land them — every non-LinkedIn social died in raw_json.
-- contacts already has these columns (0010); give staging the same three.
-- (0001's table-wide service_role grant covers new columns automatically.)

alter table staging
  add column if not exists twitter_username text,
  add column if not exists github_username text,
  add column if not exists website_url text;

-- 0005's bulk-create RPC hardcodes the contacts insert list; extend it so a
-- brand-new contact keeps its socials (the FILL path only touches contacts
-- that already exist — the creating row itself never re-runs fill).
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
    insert into contacts (full_name, "current_role", current_company, location,
                          twitter_username, github_username, website_url)
    values (item->'contact'->>'full_name',
            item->'contact'->>'current_role',
            item->'contact'->>'current_company',
            item->'contact'->>'location',
            item->'contact'->>'twitter_username',
            item->'contact'->>'github_username',
            item->'contact'->>'website_url')
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
-- re-assert the 0014 ACL pattern (create or replace keeps existing grants,
-- but be explicit so the function's policy is readable in one place)
revoke execute on function create_contacts_with_identities(jsonb) from public, anon, authenticated;
grant execute on function create_contacts_with_identities(jsonb) to service_role;
