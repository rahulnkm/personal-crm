-- migration: 0009_bulk_edit_rpcs
-- Task 2.3: bulk_add_tag RPC
--
-- Atomically appends a tag to every contact in p_ids that does not already
-- carry it, returning the ids of rows that were actually changed (idempotent).
-- Tags are kept sorted ascending so the array has a canonical form.

create or replace function bulk_add_tag(p_tag text, p_ids uuid[])
returns setof uuid
language sql
set search_path = public, extensions
as $$
  update contacts
  set tags = (select array_agg(t order by t)
              from unnest(array_append(tags, p_tag)) t),
      updated_at = now()
  where id = any(p_ids) and not (tags @> array[p_tag])
  returning id;
$$;

revoke execute on function bulk_add_tag(text, uuid[]) from public;
grant  execute on function bulk_add_tag(text, uuid[]) to   service_role;

-- ROLLBACK: drop function if exists bulk_add_tag(text, uuid[]);
