# CRM CLI — Performance Fixes (N+1 elimination) — Design

**Date:** 2026-06-14
**Status:** Approved for planning
**Companion spec:** `2026-06-14-crm-bulk-edit-design.md` (shares migration `0006` and the cohort/bulk infra)

## Problem

A performance audit of the `crm` CLI found six issues. All but one are the same
root cause: **N+1 round-trips** — code loops over rows issuing one
`client.<table>().…​.execute()` (one HTTPS round-trip to Supabase/PostgREST) per
row, where a single set-based call would do. Over a remote Supabase connection
(RTT 30–100 ms+) the cost of a loop is `N × RTT`; the fix makes it
`⌈N / chunk⌉ × RTT`. The remaining issue is a worst-case quadratic in name
clustering.

The codebase already proves the target pattern: `create_contacts_with_identities`
and `backfill_recompute_contacts` are `plpgsql` RPCs that take one `jsonb`
payload, expand it with `jsonb_to_recordset`, and run one set-based statement.
This spec extends that pattern; it does not invent a new one.

## Guiding technique (research-validated)

- **Per-row loop with DISTINCT values per row** → RPC taking `payload jsonb`,
  `update … from jsonb_to_recordset(payload) as p(<typed cols>) where …`. The
  `AS p(...)` type list is load-bearing: every jsonb value arrives as text and is
  cast per the declared types.
- **Conditional bump ("only if greater")** → same shape with the guard in the
  `WHERE` (`where p.x > c.x`); set-based and race-safe, mirrors
  `backfill_recompute_contacts`.
- **Insert-or-update against a PARTIAL unique index** (PostgREST `.upsert()`
  cannot target these) → RPC with
  `insert … select from jsonb_to_recordset(...) on conflict (cols) where <predicate> do update set …`.
- **Same value across the whole set** → no RPC needed; PostgREST
  `.update(payload).in_("id", ids)` is already one round-trip.
- Every new function: `set search_path = public`, `grant execute … to service_role`,
  and callers chunk ≤ 500 rows/call to stay under the 8 s service-role statement
  timeout (the existing `RECOMPUTE_CHUNK = 500` pattern).

## Scope — the six findings

### Finding 1 (HIGH) — dedup execute N+1
**Where:** `src/crm/commands/dedup.py` — `_execute_cluster` (lines ~94–135),
calling `_attach_identity` (36–51) and `_fill_and_log` (54–73) once per
`auto_matched` item.

**Now:** per auto-matched item, 2–5 serial round-trips (identity select, identity
insert, `select * from contacts` single, contact update, enrichment_log insert).
Items touching one contact are funnelled to a single cluster on a single thread
(to avoid write races), so a popular contact becomes a long serial chain.

**Fix:** new RPC **`attach_and_fill(payload jsonb)`** processing all
`auto_matched` items of a cluster in one call. Per item the function:
1. Looks up the existing identity by `(source, source_external_id)` (the
   `identities_source_external_id` partial unique index).
2. If an identity exists and points to a **different** contact → record the item
   as a **conflict** (do not attach, do not fill).
3. Else insert the identity if absent (against the partial unique index).
4. Apply fill-null updates to `contacts` (only columns currently null), using the
   `FILL` map (`current_role←role`, `current_company←company`, …).
5. Insert `enrichment_log` rows for fields where staged value ≠ existing non-null
   value (`method = 'import_conflict'`), and for a differing `full_name`.

**Returns:** `setof` rows `{ item_key uuid/text, outcome text }` where outcome is
`'attached'` or `'conflict'`. Python uses this to build `_patch(...)` for each
item: `attached` → `auto_matched` patch; `conflict` → `needs_review` patch with
`method='rerun_conflict'`. **Behavior contract: identical routing to the current
per-row code** — the new-contact create path (`create_contacts_with_identities`)
and the final chunked staging upsert are unchanged.

`item_key`: the staging row identity `(source, source_external_id)` carried into
and back out of the payload so Python can re-associate outcomes with items.

**Round-trips:** cluster of K auto-matches: ~3K → 1 (plus the existing create RPC
and staging upsert).

### Finding 2 (HIGH) — backfill refresh N+1
**Where:** `src/crm/commands/backfill.py` `_process_page` — the `hit` branch at
lines 121–125 fires one `interactions.update().eq("id", hit).execute()` per
already-existing interaction, inside the per-row loop.

**Now:** on any re-import / `--retry-orphans` rerun most rows already exist, so a
100-row page degrades to ~100 serial updates instead of the ~10 bulk round-trips
the module docstring promises.

**Fix:** new RPC **`bulk_upsert_interactions(payload jsonb)`** doing one
`insert … select from jsonb_to_recordset(payload) as p(...) on conflict (source,
source_external_id) where source_external_id is not null do update set
occurred_at = excluded.occurred_at, summary = excluded.summary, event_id =
excluded.event_id, contact_id = excluded.contact_id, updated_at = now()`. This
targets the `interactions_source_ext` partial unique index and handles **both**
insert and refresh in one statement.

**Consequence:** the select-first idempotency check (`existing` map, lines 98–104)
and the per-row update both disappear from `_process_page`. The function now
builds one `interactions` payload (all linked rows) + the `staging_interactions`
patch list, and issues: one `bulk_upsert_interactions` RPC + one staging upsert
per page (plus `_bulk_match` reads and `_find_or_create_event`, unchanged).

**Behavior contract:** the (source, source_external_id) arbiter resolves the same
conflicts; refresh still updates in place and never duplicates; orphans
(no contact) still get `match_status='orphaned'` and are NOT written to
`interactions`. Payload chunked ≤ PAGE (100) — already the page size.

### Finding 3 (HIGH) — event add N+1
**Where:** `src/crm/commands/log.py` `event_add` (69–100): per participant, a
resolve, an `interactions.insert()`, and `_bump_last_touchpoint` (select + update).

**Fix (no new SQL):**
- **Resolve once:** split refs into uuids vs names; resolve uuids with one
  `.in_("id", uuids)`, resolve names with the existing `_resolve` (names may be
  ambiguous and must keep their per-name error behavior). Net: one query for the
  uuid set instead of one per uuid.
- **Insert once:** build the full participant list and call
  `interactions.insert([...]).execute()` once.
- **Bump once:** all participants share `date`/`channel`/`name`. Read current
  `last_touchpoint_at` for all participant ids in one `.in_("id", ids)`; in Python
  pick the ids whose stored value is null or `< date`; issue one
  `.update({last_touchpoint_at: date, …}).in_("id", ids_to_bump)` (same payload).
- Pre-insert resolution-before-write contract (no phantom half-built event) is
  preserved: resolve all refs first, then insert event, then participants.

**Round-trips:** event of P participants: ~3P → ~4 total.

Apply the same bump helper to single `log` (`log.py:43`) so the read+update bump
lives in one shared, tested function.

### Finding 4 (MED) — review queue N+1
**Where:** `src/crm/commands/dedup.py` `review` (343–350) calling
`_candidate_display` (231–285) per queue row (2–6 reads each).

**Fix (no new SQL):** two-pass. Pass 1 collects, across the whole queue, every
candidate `contact_id` and every identity `(field, value)` to look up. Issue one
bulk `contacts` `.in_("id", all_ids)` and one bulk `contact_identities` query per
key column (`email`/`linkedin_url`/`phone`) using `.in_()`. Build dicts. Pass 2
renders each row from the in-memory maps. `_candidate_display` becomes a pure
function over the prefetched maps (testable without a client).

**Round-trips:** queue of R rows: O(R) → ~4 total.

### Finding 5 (MED) — stats 16 round-trips
**Where:** `src/crm/commands/admin.py` `stats` (74–98) — ~16 head-count queries.

**Fix:** new RPC **`crm_stats()`** returning a single `jsonb` object with all
buckets via `GROUP BY` over `contacts` (by `connection_status`, by
`closeness_tier`), `staging` (by `match_status`), `staging_interactions`
(by `match_status`), plus `contacts_total`. Python flattens it into the existing
`out` list shape (so `--json` and table output are byte-for-byte compatible) and
applies the same "drop zero rows except contacts_total" filter.

**Round-trips:** ~16 → 1.

### Finding 6 (MED) — clustering O(k²) tail
**Where:** `src/crm/clustering.py` `cluster_rows` (77–96) — name-similarity edges
are built by comparing all pairs within each trigram bucket. A *common* trigram
yields a large bucket → O(k²) within that bucket.

**Fix (no new SQL):** skip buckets larger than a configurable
`MAX_BUCKET = 200` (module constant). Pairs lost to a skipped common trigram are
still recovered through the row's rarer shared trigrams, so true matches survive.
When any bucket is skipped, `err()` a one-line notice
(`"clustering: skipped N oversized trigram bucket(s) (>200); rare-trigram edges still applied"`).
Keep the existing `tri_of` memoization and union-find short-circuit.

## Out of scope
- Changing dedup's per-cluster threading model (only the within-cluster work is
  batched).
- The single-record `contact` detail view (4 reads) — acceptable by design; only
  flagged as a latent N+1 if ever looped (no such loop exists).
- `merge`'s 3 fixed reparent updates (bounded, rare manual op).

## New SQL — migration `0006_bulk_operations.sql`
Shared with the bulk-edit spec. Functions added by THIS spec:
`attach_and_fill(payload jsonb) returns table(...)`,
`bulk_upsert_interactions(payload jsonb) returns void`,
`crm_stats() returns jsonb`. Each: `set search_path = public`,
`grant execute … to service_role`. (The bulk-edit spec adds `bulk_add_tag` and
`bulk_append_note` to the same migration.)

## Testing
- Local Supabase stack only (existing `conftest.py` fixture; refuses non-local URLs).
- Per finding: a behavior test proving output/routing is unchanged vs the old
  path, plus a **round-trip regression test** — wrap/spy the client to assert the
  bulk path issues ONE RPC (or the documented constant number of calls), not N.
  E.g. monkeypatch `client.rpc` / `client.table(...).update` to count calls.
- `attach_and_fill`: cases for fill-null, conflict→needs_review routing, identity
  already present for the same contact (no-op insert), absent identity (insert).
- `bulk_upsert_interactions`: insert path, refresh-in-place path (no duplicate),
  orphan exclusion, idempotency on rerun.
- `crm_stats`: parity with the old per-bucket counts on a seeded DB.
- `cluster_rows`: oversized-bucket skip still clusters true matches; notice emitted.
- `event_add` / `log`: bump-only-if-greater correctness; multi-participant batch.

## Success criteria
- All six findings fixed; behavior contracts hold (existing tests stay green).
- New + changed code at 100% line coverage (`pytest-cov`).
- Benchmark (`scripts/bench_bulk.py`, shared with bulk-edit spec) shows the
  round-trip reduction and wall-clock improvement on a seeded N.
