-- 0010 created enrich_review + candidate_identities without the deny-all RLS
-- every other table got in 0001/0003; close the gap and lock function ACLs.

alter table enrich_review enable row level security;
alter table candidate_identities enable row level security;

grant all on table enrich_review to service_role;
grant all on table candidate_identities to service_role;

-- 0010-0013 functions kept Postgres's default execute-to-PUBLIC grant;
-- match the 0006/0008/0009 pattern: service_role only.
revoke execute on all functions in schema public from public, anon, authenticated;
grant execute on all functions in schema public to service_role;

-- stop the default grant recurring for functions created by future migrations
alter default privileges in schema public revoke execute on functions from public;
