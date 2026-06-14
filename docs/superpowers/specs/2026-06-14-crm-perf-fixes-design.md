# CRM CLI — Performance Fixes (N+1 elimination) — Design

**Date:** 2026-06-14
**Status:** Approved for planning (revised after 2nd adversarial review)
**Companion spec:** `2026-06-14-crm-bulk-edit-design.md`
**Sequencing:** this spec lands FIRST — it creates migration `0006` and the shared
`_bump_last_touchpoint_bulk` helper that the bulk-edit spec consumes. Bulk-edit adds migration `0008` (no shared file → safe). NOTE: a `0007_recompute_clear_empty.sql` was added during impl to make recompute null abandoned contacts (denorm heal).

## Problem

A performance audit found six issues in the `crm` CLI. Five share one root cause:
**N+1 round-trips** — a loop over rows issues one
`client.<table>()…​.execute()` (one HTTPS round-trip to Supabase/PostgREST) per
row, where a single set-based call would do. The sixth is a worst-case quadratic
in name clustering.

### Honest framing of the win (revised)
The headline metric is **round-trip count**, not wall-clock. Each `.execute()` is
one network round-trip; the cost of a loop is `N × RTT`. Against a **local**
Supabase (the default dev/test target) RTT ≈ 0–1 ms, so wall-clock barely moves —
the real, measurable improvement locally is the **call-count reduction** (e.g.
"event with 50 participants: ~150 calls → 4"). The wall-clock win is felt on
**remote** Supabase (README documents a cloud deploy; RTT 30–100 ms) and inside
**agent loops** that call these commands repeatedly (see
`docs/operational-loads.md`). We do NOT claim a dramatic local wall-clock number;
we report round-trip reduction as the primary figure and a latency-injected
wall-clock run as the remote-equivalent (see Benchmark).

## Technique selection (revised)

Prefer the **simplest tool that preserves the existing, already-tested behavior**:

- **Batch the READS client-side** and keep branching logic in Python when that
  logic is subtle and already covered by tests (Finding 1). One bulk `.in_()`
  read replaces N per-row reads; the Python keeps byte-identical routing.
- **Same value across the set** → PostgREST `.update(payload).in_("id", ids)` in
  one round-trip (Finding 3 bump). No RPC.
- **RPC (plpgsql)** ONLY where PostgREST genuinely cannot express the operation:
  a **partial-index** conflict target (Finding 2) or a set-based aggregate
  (Finding 5). RPCs take one `payload jsonb`, expand with `jsonb_to_recordset`,
  run one statement. Every value in jsonb arrives as **text** and is cast per the
  `AS p(...)` type list — that list is load-bearing (declare `uuid`/`date`/
  `timestamptz`/enum/`jsonb` correctly or the call aborts with a cast error).
- Every new function: `set search_path = public, extensions` (repo convention),
  `grant execute … to service_role`, and a `drop function if exists …` rollback
  line recorded in the spec/PR (free tier has no backups). Callers chunk so no
  single statement risks the 8 s service-role statement timeout.

## Scope — the six findings

### Finding 1 (HIGH) — dedup execute N+1 → CLIENT-SIDE BATCH (no RPC)
**Where:** `src/crm/commands/dedup.py` — `_execute_cluster` (~94–135) calling
`_attach_identity` (36–51) and `_fill_and_log` (54–73) once per `auto_matched`
item: per item ~3–5 round-trips (identity select, identity insert, `select *`
single contact, contact update, enrichment_log insert), serialized because
`_union_by_existing_contact` funnels all items touching one contact onto one
cluster/thread.

**Why NOT an RPC (decided in review):** a set-based `UPDATE … FROM` cannot
reproduce the current **serial, in-order** semantics. Today item N re-reads the
contact *after* items 1…N-1 wrote it, so the first fill of a null column wins and
a later differing value is logged as an `import_conflict`. A set statement
evaluates every row against the pre-statement snapshot (so it logs zero
conflicts) and, with duplicate join keys, Postgres updates the target once with a
**nondeterministic** source row. That silently changes data and drops provenance
for exactly the multi-row-same-contact clusters the union step creates. A plpgsql
serial loop could mimic it but would have to perfectly mirror Python routing —
standing drift risk for marginal gain at this scale.

**Fix — batch reads, fold in Python, batch writes. Per cluster:**
1. **One** `contacts` read: `select * … .in_("id", contact_ids)` for all distinct
   target contact ids in the cluster (ids already known after `create` RPC +
   `deref`). Build `contact_by_id`.
2. **One** `contact_identities` read per identity key the items use, batched with
   `.in_()`, to resolve the existing-identity / `rerun_conflict` check.
3. **Fold in Python, in plan order** (preserving today's semantics exactly):
   maintain an in-memory accumulator of each contact's evolving column state.
   For each item: apply the existing `_attach_identity` conflict rule (existing
   identity → different contact ⇒ outcome `conflict`, route to `needs_review`
   with `method='rerun_conflict'`, `resolved=False`, **no** `match_confidence` —
   matching dedup.py:125); else outcome `attached`. For attached items, compute
   fill-null updates and `import_conflict` rows against the **accumulator** (so a
   later item sees an earlier item's fill, identical to the serial loop), using
   the `FILL` map (`current_role←role, current_company←company, location←location`)
   and the `full_name` conflict rule (dedup.py:65–66).
4. **Batch the writes:** one `contact_identities.insert([...])` for absent
   identities; one `enrichment_log.insert([...])` for all conflict rows; and the
   fill-null updates as **one update per distinct contact** (typically one — the
   cluster is usually a single existing contact). Outcomes are keyed by staging
   **row id** (PK-unique), never `(source, source_external_id)` (which can repeat
   in a payload), so Python re-associates outcomes unambiguously.

**Behavior contract (must be pinned by tests, not prose):** the `_patch(...)`
dict for each outcome is byte-identical to today — `attached` →
`auto_matched` patch carrying `match_confidence`; `conflict` → `needs_review`
patch with `method='rerun_conflict'`, no `resolved_at`, no `match_confidence`. The
create path (`create_contacts_with_identities`) and the final chunked staging
upsert are unchanged. Same-contact existing identity ⇒ no re-insert but fill +
conflict-log still run (matches `_attach_identity` returning True). A row may
auto-match a sibling created in the same cluster — Python derefs
`matched_ref`→uuid (via `keymap`) BEFORE building the read set.

**Round-trips:** cluster of K auto-matches: ~3K → ~5 (create RPC + 2 reads + 2
bulk inserts + 1 update/contact). **Concurrency:** threading model unchanged;
the per-cluster single-thread invariant still prevents cross-cluster contention
on a shared contact (union-by-existing-contact guarantees one contact lives in
one cluster).

### Finding 2 (HIGH) — backfill refresh N+1 → `bulk_upsert_interactions` RPC
**Where:** `backfill.py` `_process_page` — the `hit` branch (121–125) fires one
`interactions.update().eq("id", hit).execute()` per already-existing interaction,
inside the per-row loop. On any re-import / `--retry-orphans` rerun most rows
exist → a 100-row page degrades to ~100 serial updates.

**Fix:** new RPC **`bulk_upsert_interactions(payload jsonb) returns void`**:
```
insert into interactions
  (contact_id, event_id, kind, channel, occurred_at, summary,
   logged_by, source, source_external_id)
select p.contact_id, p.event_id, p.kind, p.channel, p.occurred_at, p.summary,
       p.logged_by, p.source, p.source_external_id
from jsonb_to_recordset(payload) as p(
  contact_id uuid, event_id uuid, kind interaction_kind, channel text,
  occurred_at date, summary text, logged_by text, source text,
  source_external_id text)
on conflict (source, source_external_id) where source_external_id is not null
do update set occurred_at = excluded.occurred_at, summary = excluded.summary,
              event_id = excluded.event_id, contact_id = excluded.contact_id,
              updated_at = now();
```
This targets the partial unique index `interactions_source_ext`
(`where source_external_id is not null`) — which PostgREST `.upsert()` cannot
express, hence the RPC. **`DO UPDATE` touches only the 5 mutable columns**
(matching today's refresh); `kind`/`channel`/`logged_by` are set on insert and
**never overwritten** on refresh.

**NULL-safety contract:** rows with `source_external_id IS NULL` bypass the
partial index → `ON CONFLICT` can't fire → they'd insert duplicates on every
rerun. This is safe ONLY because `staging_interactions.source_external_id` is
`NOT NULL` (schema 0003:9), so backfill never produces such a row. The RPC
documents this; the payload builder asserts non-null. (`kind`/`logged_by` are
`NOT NULL`/FK — always present in the payload.)

**Python changes:** `_process_page` drops the select-first `existing` map
(98–104) and the per-row update. It builds one `interactions` payload (linked
rows only — orphans excluded in Python and still patched `match_status='orphaned'`)
and the staging patch list, then issues: one `bulk_upsert_interactions` RPC + one
staging upsert per page. The `linked`/`orphaned` counters are still computed in
Python from the loop (unchanged). `touched.add(contact_id)` must still fire for
**refreshed** rows, not just new inserts (preserves recompute coverage). Payload
chunked ≤ PAGE (100).

**Denorm-staleness fix (decided after review):** the `DO UPDATE` re-points
`contact_id`; the OLD contact's `last_touchpoint_*` denorm would go stale and was
never recomputed (a pre-existing latent bug — the Python select-first map that
could have detected it is being deleted). So `bulk_upsert_interactions`
`returns setof uuid` = the **prior** `contact_id`s of rows whose `contact_id`
actually moved (via a CTE comparing the pre-update value to `excluded.contact_id`).
`_process_page` unions those into `touched` so `_recompute` heals both the new and
the abandoned contact. This makes the module's "recompute heals denorm staleness"
claim actually true.

### Finding 3 (HIGH) — event add N+1 → bulk insert + shared bulk bump
**Where:** `log.py` `event_add` (69–100): per participant a resolve, an
`interactions.insert()`, and `_bump_last_touchpoint` (select + update).

**Fix (no new SQL):**
- **Resolve once:** split refs into uuids vs names; resolve uuids with one
  `.in_("id", uuids)`; resolve names via existing `_resolve` (keep per-name
  ambiguity errors). Pre-insert resolve-before-write contract preserved.
- **Insert once:** `interactions.insert([...])` for all participants.
- **Bump once — new shared helper `_bump_last_touchpoint_bulk(client, ids, occurred, channel, topic)`** (also used by single `log` and by `crm bulk log`). **It delegates to a server-side RPC** (decided after review — a client-side read-then-write is a TOCTOU lost-update: a concurrent `crm log` writing a newer date could be overwritten by this bulk write's stale snapshot, across the whole cohort). The helper:
  - if `occurred` is None or `ids` empty → return (matches `_bump_last_touchpoint`).
  - calls the new RPC **`bulk_bump_last_touchpoint(p_ids uuid[], p_occurred date, p_channel text, p_topic text)`** (migration 0006), chunked by `CHUNK` over `ids`. The RPC does ONE guarded statement:
    `update contacts set last_touchpoint_at = p_occurred, last_touchpoint_channel = p_channel, last_touchpoint_topic = p_topic, updated_at = now() where id = any(p_ids) and (last_touchpoint_at is null or last_touchpoint_at < p_occurred)`.
    The `< p_occurred` guard is re-evaluated server-side under the row lock, so a concurrently-written newer value is never clobbered (**equal date is a no-op**, matching today's `>=` early-return). This mirrors `bulk_add_tag`'s proven single-statement read-modify-write, fixes the **pre-existing** single-`log` race for free, and removes the read round-trips entirely.
  - `CHUNK` is a monkeypatchable module constant (in `src/crm/bulk.py`) so boundary tests use small values.

Refactor single `log` (log.py:43) to call `_bump_last_touchpoint_bulk(client, [contact_id], …)` so the one bump path is shared and race-safe. (Single `log` now needs migration 0006 applied — fine, 0006 lands first.)
- Refactor single `log` (log.py:43) to call the same helper.

**Round-trips:** event of P participants: ~3P → ~4.

### Finding 4 (MED) — review queue N+1 → two-pass batch (no SQL)
**Where:** `dedup.py` `review` (343–350) calling `_candidate_display` (231–285)
per row (2–6 reads each).

**Fix:** two-pass. Pass 1 collects, across the whole queue, every candidate
`contact_id` and every identity `(field, value)` to look up — **applying the same
`_is_role_email(val)` skip** the current code uses (dedup.py:248) so we don't
over-fetch. Issue one bulk `contacts` `.in_("id", all_ids)` and one
`contact_identities` `.in_()` per key column. Pass 2 renders from in-memory maps.
`_candidate_display` becomes a pure function over the prefetched maps (testable
without a client). Round-trips: O(R) → ~4.

### Finding 5 (MED) — stats 16 round-trips → `crm_stats()` RPC
**Where:** `admin.py` `stats` (74–98) — ~16 head-count queries.

**Fix:** new RPC **`crm_stats() returns jsonb`** returning one object with all
buckets via `GROUP BY` over `contacts` (by `connection_status`, by
`closeness_tier`), `staging` (by `match_status`), `staging_interactions` (by
`match_status`), plus `contacts_total`. Counts cast to **int** (not bigint→float)
so `--json` renders `3` not `3.0`.

**Python flattening (parity is load-bearing):** iterate the SAME fixed literal
lists the current code uses (`in_network/contact_on_file`; the 5 tiers; staging
`pending/auto_matched/needs_review/merged/rejected`; touchpoints
`pending/linked/orphaned`), defaulting any bucket the `GROUP BY` omitted to 0,
then apply the existing filter `if count or metric == 'contacts_total'` (so a
zero `contacts_total` still shows, other zeros drop). This reproduces the exact
ordered `out` list. A parity test asserts full ordered-list equality on a seeded
DB, including a dropped zero-bucket and a kept `contacts_total`. Round-trips:
~16 → 1.

### Finding 6 (MED) — clustering O(k²) tail → bucket cap (no SQL)
**Where:** `clustering.py` `cluster_rows` — the **name-similarity** loop (87–96)
compares all pairs within each trigram bucket; a common trigram → large bucket →
O(k²). (The exact-key buckets at 68–76 are O(k), untouched.)

**Fix:** skip name-sim buckets larger than module constant `MAX_BUCKET = 200`.
When any are skipped, `err()` exactly:
`"clustering: skipped N oversized trigram bucket(s) (>200); rare-trigram edges still applied"`.
Keep `tri_of` memoization and the union-find short-circuit.

**Recovery is a heuristic, not lossless:** two names sharing ONLY an oversized
trigram (no rarer shared trigram) are dropped — acceptable, vanishingly rare in a
personal network, and surfaced by the notice. Reframe: this is a **robustness
guard against a pathological import**, not a perf win.

## Out of scope
- The dedup per-cluster threading model (only within-cluster work changes).
- The single-record `contact` detail view (4 reads, by design).
- `merge`'s 3 fixed reparent updates (bounded, rare).

## New SQL — migration `0006_perf_rpcs.sql`
Functions: `bulk_upsert_interactions(payload jsonb) returns setof uuid` (moved old
contact_ids), `crm_stats() returns jsonb`, and
`bulk_bump_last_touchpoint(p_ids uuid[], p_occurred date, p_channel text, p_topic text) returns void`.
Each: `set search_path = public, extensions`, then BOTH
`revoke execute … from public` (defense-in-depth — Postgres grants EXECUTE to
PUBLIC by default; RLS is the only backstop otherwise) AND
`grant execute … to service_role`. Record a `drop function if exists` rollback
line per function in the PR. **No new index needed** — `bulk_upsert_interactions`
uses the existing `interactions_source_ext` partial unique index; the bump uses
the contacts PK; `crm_stats` GROUP BYs scan small tables. (The `attach_and_fill`
RPC from the prior draft is REMOVED — Finding 1 is now client-side.)

**No statement_timeout change needed** for cloud deploy: every RPC payload is
chunked (≤ PAGE/CHUNK) well under the 8 s service-role limit. New RPCs ship via
`supabase db push` to any cloud project (each carries its own grant, since the
historical blanket `grant … on all functions` only covered functions existing at
0001/0002).

## Testing
Local Supabase stack only (`conftest.py` fixture; refuses non-local URLs).

**Infra (gates the coverage claim):**
- Add `pytest-cov` AND `diff-cover` to the `[dependency-groups] dev` list in
  `pyproject.toml` (the project uses `uv`; `uv.lock` is regenerated). Add
  `[tool.coverage.run] source = ["crm"]`, `branch = true`. Run
  `pytest --cov=crm --cov-report=xml --cov-report=term-missing` (the **xml** report
  is what `diff-cover` consumes). "100% on changed code" is enforced by
  `diff-cover coverage.xml --compare-branch=main --fail-under=100` (whole-repo
  100% is out of scope). Document both commands in the PR.
- **Migration application:** the new RPCs require `0006` (plus `0007` recompute-heal, and bulk-edit's `0008`)
  applied. Document a preflight: `supabase db reset` before the suite (or
  `supabase migration up`). `conftest.py` only truncates `DATA_TABLES`; add a note
  that migrations must be applied first. CI/local loop runs the reset once.
- **plpgsql is invisible to coverage tools.** Each RPC's internal branches are
  pinned by **behavioral DB-assertion tests** (seed → call → assert resulting
  rows field-by-field), enumerated below — this is the real coverage contract for
  the SQL, distinct from the Python line-coverage number.
- **Round-trip regression spy:** monkeypatch the per-module imported binding
  `crm.commands.dedup.get_client` / `crm.commands.backfill.get_client` (NOT
  `crm.config.get_client` — both modules do `from crm.config import get_client`,
  and workers call it per-thread, so the patch must hit the imported name). Use a
  **new** chaining-aware counting proxy built fresh in `tests/_spy.py` — note the
  existing `_Proxy` in `test_contacts.py` is a *fault-injection delegator* (it
  swaps one table for a raising stub), NOT a call counter, so it's the structural
  shape to follow (table-dispatch + `__getattr__` passthrough) but the counting is
  net-new: wrap each builder method, return self for chaining, increment a counter
  keyed at the terminal `.execute()` (and at `.rpc()`). Ships with
  `# pragma: no cover`.
  Document the EXACT expected call counts per command, enumerated precisely (not
  glossed):
  - dedup cluster: 1 `create_contacts_with_identities` RPC + 1 `contacts` read +
    1 `contact_identities` read + 1 `contact_identities` insert + 1
    `enrichment_log` insert + N_distinct_contacts `contacts` updates + the chunked
    staging upsert.
  - backfill page: `_claim_page` (1 select + 1 update) + `_bulk_match` (≤1 read
    per MATCH_KEY per value-chunk) + `_find_or_create_event` (1–2 per unique
    event) + 1 `bulk_upsert_interactions` RPC + 1 staging upsert; then `_recompute`
    (1 RPC per `RECOMPUTE_CHUNK`) after workers drain. (Backfill has three distinct
    chunk constants — `PAGE=100`, `RECOMPUTE_CHUNK=500`, and the payload chunk —
    use the right one when computing expected counts.)
- **Monkeypatchable chunk/page constants** (e.g. `monkeypatch.setattr(mod, "PAGE", 2)`,
  same trick as `test_import_linkedin.py`'s `MAX_MEMBER_BYTES`) so boundary tests
  use 2/3 rows, not 100/500. Bulk-seed large fixtures via one `insert([...])`.
- **Fault-injection / pragma:** the `_find_or_create_event` 23505 retry branch
  and the dedup/backfill worker `except`/`Exit(1)` paths are race-only — cover via
  a monkeypatched `insert` raising a fake `APIError(code="23505")` / forced
  exception, or mark `# pragma: no cover` with justification. Decide per branch in
  the plan; no silent gaps.

**Behavioral cases:**
- Finding 1: pin both outcome patches exactly (status, method, presence/absence of
  `resolved_at` and `match_confidence`); fill-null vs `import_conflict` per field;
  `full_name` conflict; same-contact existing identity ⇒ no re-insert but fill +
  log still run; two items filling the same null col → earlier wins, later logs a
  conflict (the serial-semantics regression guard); a row auto-matching a sibling
  created in the same cluster.
- Finding 2: insert path; refresh-in-place (no duplicate; `kind`/`channel`/
  `logged_by` unchanged); orphan excluded from payload; idempotency on rerun;
  payload non-null `source_external_id` assertion.
- Finding 3: multi-participant batch; equal-date no-op; empty `ids_to_bump` skip;
  None-date event skip.
- Finding 4: parity render vs current `_candidate_display`; role-email skip.
- Finding 5: full ordered-list equality on seeded DB (dropped zero, kept
  `contacts_total`); int (not float) counts.
- Finding 6: oversized-bucket skip still clusters true matches (which share a rare
  trigram); exact notice text; negative branch (no notice when all buckets ≤ 200).

## Benchmark — `scripts/bench_bulk.py`
- **Primary, headline metric: round-trip count ratios**, exact integers from the
  counting proxy that a reviewer can verify: backfill refresh 100 rows: **~101 → 1**;
  event add 50 participants: **~150 → ~4**; stats: **16 → 1**; dedup cluster of K
  auto-matches on one contact: **~3K → ~5**; bulk verbs N ids: **N → ⌈N/CHUNK⌉**.
  Honest on the local stack where RTT ≈ 0. **Lead the PR with this table.**
- **Remote projection (not a measurement):** report ONE derived line per fix —
  "at an assumed 50 ms RTT: ~Xs → ~Ys, computed from the round-trip counts." Do
  NOT report median/p90 of injected-sleep wall-clock — with a fixed per-call sleep
  that number is exactly `count × sleep` (circular theater). Round-trip count is
  the falsifiable metric; latency is a projection, labeled as such.
- The old per-row path is deleted, so the benchmark includes a **reference naive
  loop re-implemented inline in the script** (NOT in `crm/`), each loop commented
  with the exact old line range it mirrors (backfill.py:121-125 etc.) so it's a
  faithful baseline, not a strawman. Net-new bulk verbs use a loop labeled "naive
  baseline" (no prior impl existed).
- Methodology: fixed seed chosen so every refresh row is a `hit` and every
  participant is bump-eligible (else the ratios don't reproduce); truncate data
  tables between runs (not full `db reset`); one warm-up discard.

## Success criteria
- All six findings fixed; behavior contracts pinned by assertions (existing tests
  stay green).
- New/changed code at 100% line coverage via `diff-cover` vs `main`; RPC SQL
  branches covered by behavioral DB tests.
- `scripts/bench_bulk.py` reports round-trip reductions (primary) + remote-
  equivalent wall-clock (median/p90), honestly labeled.
