-- supabase/migrations/0007_enrichment_provenance_backfill.sql
create or replace function enrich_seed_provenance() returns void language plpgsql as $$
declare f text; cols text[] := array['current_role','current_company','location',
  'company_category','company_description','company_domain',
  'avatar_url','github_username','twitter_username','website_url']; begin
  foreach f in array cols loop
    execute format($f$
      insert into enrichment_log (contact_id, field, new_value, source, confidence, method, is_current)
      select c.id, %L, c.%I,
             case when exists(select 1 from enrichment_log e where e.contact_id=c.id and e.field=%L and e.method='manual_set')
                  then 'rahul' else 'legacy' end,
             case when exists(select 1 from enrichment_log e where e.contact_id=c.id and e.field=%L and e.method='manual_set')
                  then 1.0 else 0.8 end,
             case when exists(select 1 from enrichment_log e where e.contact_id=c.id and e.field=%L and e.method='manual_set')
                  then 'manual_set' else 'legacy_import' end,
             true
      from contacts c
      where c.%I is not null
        and not exists (select 1 from enrichment_log e2 where e2.contact_id=c.id and e2.field=%L and e2.is_current)
    $f$, f, f, f, f, f, f, f);
  end loop;
end $$;
-- run once now; safe/idempotent (guarded by the not-exists is_current check)
select enrich_seed_provenance();
