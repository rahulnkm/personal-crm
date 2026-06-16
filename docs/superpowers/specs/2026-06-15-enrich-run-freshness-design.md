# `crm enrich run` fixes + Freshness clock — Design

**Date:** 2026-06-15 · **Status:** approved design, pre-implementation · Plan 2 items #2 + #3.

## Goal
Two isolated units: (#2) fix `crm enrich run` scoping/counting; (#3) add a per-field freshness clock so stale enrichments resurface for re-enrichment.

## Existing state
- `crm enrich run` (`src/crm/commands/enrich.py`): walks in_network contacts closeness-first, runs deterministic sources, applies via the RPCs. Bugs: `--limit` counts only enriched contacts (not touched); no `--all` (in_network-only); review-only contacts mislabeled `no_signal` in the summary.
- `enrichment_log.refresh_after` (date) column exists (migration 0006) but is **never written** — read in 3 places (`contacts.py`, `enrich.py`, `retrieval.py`), always NULL. The scalar RPC `enrich_apply_candidate` (0006) inserts provenance rows without it. (Stamping it retroactively activates those 3 existing readers + `stats` — no new read code needed.)
- **Migration collision (verified):** main HEAD has 0001–**0009** (`0006_perf_rpcs`, `0007_recompute_clear_empty`, `0008_bulk_insert_identities`, `0009_bulk_edit_rpcs`) — main advanced past our merge-base (0005). Our branch's `0006_enrichment_substrate` / `0007_enrichment_provenance_backfill` / `0008_enrich_array` collide *by number* with main's. **Rebase reconciliation:** renumber our three to `0010`/`0011`/`0012` (content unchanged; cloud already has them applied idempotently via psql, and tests call by function name not filename). The new freshness migration is therefore **`0013_enrich_freshness.sql`** (gap until rebase is fine — ordering is lexicographic).

## #2 — `crm enrich run` (Python only, no schema)
- **`--limit N`**: after building + closeness-sorting the candidate list, slice `contacts[:N]` before the loop → caps contacts *processed*. Replaces the post-hoc enriched-only counter.
- **`--all`**: boolean flag; when set, omit the `connection_status` filter in `_candidate_contacts` (default stays in_network-only).
- **Summary**: add a `reviewed` counter. Per-contact outcome classification: if any field returned `review` and none returned `golden`/`added` → status `reviewed` (not `no_signal`). Keep `enriched / already_fresh / no_signal / no_email / error`.

## #3 — Freshness clock (migration 0009 + Python)
**SQL helper** (centralizes TTL policy; both RPCs call it):
```sql
create or replace function enrich_refresh_after(p_field text, p_method text)
returns date language sql immutable as $$
  select case
    when p_method = 'manual_set' then null              -- manual never expires
    when p_field in ('current_role','current_company','company_category') then current_date + 90
    when p_field in ('location','email_status') then current_date + 180
    else null                                            -- origin_context, expertise, interests, socials… never
  end;
$$;
```
(`email_status` is inert today — it's only written via manual `crm set`, not the enrich RPC — but the branch is kept for when an email-verify source writes it through `enrich_apply_candidate`.)

**RPC change (0013):** **only `enrich_apply_candidate`** sets `refresh_after := enrich_refresh_after(p_field, p_method)` on the provenance INSERT — `create or replace`, no signature change, additive/safe. **`enrich_apply_array` is intentionally NOT changed:** array rows are `is_current=false` (excluded from the `due` predicate) and every array enrich field (`expertise`/`interests`) resolves to the NULL/never branch anyway, so stamping it would be dead weight.

**`crm enrich due`:** lists contacts with `EXISTS (enrichment_log WHERE contact_id=c.id AND is_current AND refresh_after < current_date)`, ordered by closeness_tier (t1>t2>t3>none); `--json`. (Array provenance has no `is_current`, so freshness is scalar-only by design.)

**`crm enrich run --due`:** restrict the candidate set to due contacts (same EXISTS predicate).

**Clock starts now:** pre-existing rows have `refresh_after = NULL` ⇒ never "due" ⇒ no flood; only newly-written volatile fields age. Intentional.

## Testing (TDD)
- `enrich_refresh_after`: +90 for volatile, +180 for location/email_status, NULL for stable + any manual_set (test via `db.rpc`).
- Both RPCs stamp `refresh_after` on insert (volatile non-null, stable null, manual null).
- `crm enrich due`: surfaces a contact with a past `refresh_after`, excludes a fresh one, orders by closeness; `run --due` processes only due contacts.
- `crm enrich run`: `--limit N` processes exactly N; `--all` reaches a contact_on_file contact; a review-only contact is summarized `reviewed`.
- Local stack (55322), psql-apply 0009 (db reset blocked); full suite green.

## Files
- `supabase/migrations/0013_enrich_freshness.sql` — `enrich_refresh_after` + `create or replace` of `enrich_apply_candidate` only.
- `src/crm/commands/enrich.py` — run `--limit`/`--all`/`reviewed`; `due` command; `run --due`.
- `tests/test_enrich_freshness.py` (new); extend `tests/test_enrich_run.py`.

## Merge reconciliation (for the rebase-to-main goal, not this unit's tests)
Before merge: `git mv` `0006_enrichment_substrate`→`0010`, `0007_enrichment_provenance_backfill`→`0011`, `0008_enrich_array`→`0012` (content unchanged) so the enrichment series sits above main's `0009`; restore the `config.toml` port-hack; commit the array-write work. Handled in finishing-a-development-branch, after #2+#3 land.

## Out of scope
Array-field freshness (no is_current). Backfilling refresh_after onto existing rows. The durable research-agent skill (Plan 2 #1, next).
