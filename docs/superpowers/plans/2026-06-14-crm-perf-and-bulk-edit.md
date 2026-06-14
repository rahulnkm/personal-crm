# CRM Performance Fixes + Bulk-Edit Commands — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Eliminate six audited N+1 / perf issues in the `crm` CLI and add a `crm bulk set/tag/log` command family, with behavior preserved and 100% coverage on changed code.

**Architecture:** Replace per-row DB round-trips with (a) client-side read-batching + Python folds where subtle logic must be preserved, (b) one bulk `.update().in_()` where the value is uniform, and (c) plpgsql RPCs only where PostgREST can't express the op (partial-index upsert; GROUP BY aggregate; atomic array-append). Two new migrations: `0006_perf_rpcs.sql`, `0008_bulk_edit_rpcs.sql`.

**Tech Stack:** Python 3 · Typer · supabase-py (PostgREST) · Postgres (Supabase, local stack) · pytest · pytest-cov + diff-cover · uv.

**Source specs (read both before starting):**
- `docs/superpowers/specs/2026-06-14-crm-perf-fixes-design.md`
- `docs/superpowers/specs/2026-06-14-crm-bulk-edit-design.md`

**Global rules:**
- TDD: failing test → run-fail → implement → run-pass → commit. One logical change per commit.
- All tests run against the LOCAL Supabase stack only (`conftest.py` refuses non-local URLs).
- **Preflight before any DB test run:** `supabase db reset` to apply migrations (incl. new 0006, 0007 recompute-heal, 0008). `conftest.py` only truncates data tables; it does not create schema.
- plpgsql is invisible to coverage — every RPC branch is pinned by a **behavioral DB test** (seed → call → assert rows), following the existing `tests/test_dedup_rpcs.py` pattern.
- Commit message convention: end with `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`.

---

## File Structure

**Create:**
- `supabase/migrations/0006_perf_rpcs.sql` — `bulk_upsert_interactions`, `crm_stats`
- `supabase/migrations/0008_bulk_edit_rpcs.sql` — `bulk_add_tag`
- `src/crm/bulk.py` — `CHUNK`/`PAGE` constants, `_resolve_cohort`, cohort gate/dry-run/json/confirm helpers, chunked-write helper
- `src/crm/commands/bulk.py` — `bulk` Typer sub-app: `set`, `tag`, `log`
- `tests/_spy.py` — chaining-aware counting proxy (`# pragma: no cover`)
- `scripts/bench_bulk.py` — round-trip + injected-latency benchmark
- Test files: `tests/test_perf_rpcs.py`, `tests/test_clustering_cap.py`, `tests/test_bulk_cohort.py`, `tests/test_bulk_set.py`, `tests/test_bulk_tag.py`, `tests/test_bulk_log.py`, `tests/test_spy.py`

**Modify:**
- `src/crm/commands/backfill.py` — Finding 2 (use `bulk_upsert_interactions`)
- `src/crm/commands/admin.py` — Finding 5 (use `crm_stats`)
- `src/crm/commands/log.py` — Finding 3 (`event_add` batch + new `_bump_last_touchpoint_bulk`; refactor single `log`)
- `src/crm/commands/dedup.py` — Finding 1 (client-side batch fold), Finding 4 (review two-pass)
- `src/crm/clustering.py` — Finding 6 (bucket cap)
- `src/crm/commands/contacts.py` — refactor `list_contacts` onto `_resolve_cohort`
- `src/crm/cli.py` — register `bulk` sub-app
- `pyproject.toml` — `pytest-cov`, `diff-cover`, coverage config
- `README.md` — document `crm bulk` verbs

---

## Phase 0 — Shared infrastructure

### Task 0.1: Coverage tooling

**Files:** Modify `pyproject.toml`

- [ ] **Step 1: Add dev deps + coverage config.** Add `pytest-cov` and `diff-cover` to the `[dependency-groups] dev` list. Append:
```toml
[tool.coverage.run]
source = ["crm"]
branch = true
```
- [ ] **Step 2: Sync + verify.** Run `uv sync` then `uv run pytest --cov=crm --cov-report=term-missing -q`.
Expected: suite runs (some coverage % printed; existing tests pass).
- [ ] **Step 3: Commit.** `git add pyproject.toml uv.lock && git commit` — `build: add pytest-cov + diff-cover for coverage gating`.

### Task 0.2: Counting proxy for round-trip regression tests

**Files:** Create `tests/_spy.py`, `tests/test_spy.py`

The proxy wraps a real client, returns itself for builder calls, and counts terminal `.execute()` / `.rpc()` calls by table+op. Patch target is the per-module imported binding (e.g. `crm.commands.dedup.get_client`).

- [ ] **Step 1: Write the proxy test (failing).** `tests/test_spy.py`:
```python
from tests._spy import CountingClient

class _FakeBuilder:
    def __init__(self, sink, table): self._sink, self._table = sink, table
    def select(self, *a, **k): return self
    def update(self, *a, **k): self._op = "update"; return self
    def insert(self, *a, **k): self._op = "insert"; return self
    def in_(self, *a, **k): return self
    def eq(self, *a, **k): return self
    def execute(self):
        self._sink.append((self._table, getattr(self, "_op", "select")))
        class R: data = []
        return R()

class _FakeClient:
    def __init__(self): self.calls = []
    def table(self, name): return _FakeBuilder(self.calls, name)
    def rpc(self, name, params):
        self.calls.append(("rpc", name))
        class B:
            def execute(self_inner):
                class R: data = []
                return R()
        return B()

def test_counts_table_ops_and_rpc():
    spy = CountingClient(_FakeClient())
    spy.table("contacts").update({"a": 1}).in_("id", [1]).execute()
    spy.table("contacts").insert([{"a": 1}]).execute()
    spy.rpc("crm_stats", {}).execute()
    assert spy.count("contacts", "update") == 1
    assert spy.count("contacts", "insert") == 1
    assert spy.rpc_count("crm_stats") == 1
    assert spy.total() == 3
```
- [ ] **Step 2: Run, verify fail.** `uv run pytest tests/test_spy.py -v` → FAIL (no module).
- [ ] **Step 3: Implement `tests/_spy.py`.**
```python
# pragma: no cover  (test helper — exercised indirectly by regression tests)
from collections import Counter


class _BuilderProxy:
    """Delegates every builder method to the real builder, returning self for
    chaining, and records (table, op) at the terminal .execute()."""
    def __init__(self, real_builder, table, sink, latency=0.0):
        self._real, self._table, self._sink, self._latency = real_builder, table, sink, latency
        self._op = "select"

    def __getattr__(self, name):
        attr = getattr(self._real, name)
        if not callable(attr):
            return attr

        def wrapped(*args, **kwargs):
            if name in ("update", "insert", "upsert", "delete"):
                self._op = name
            result = attr(*args, **kwargs)
            if name == "execute":
                if self._latency:
                    import time
                    time.sleep(self._latency)
                self._sink.append((self._table, self._op))
                return result
            # builder methods return the real builder; keep proxying it
            self._real = result
            return self
        return wrapped


class CountingClient:
    """Wrap a real supabase client; count terminal table ops and rpc calls.
    `latency` (seconds) is injected per .execute() for remote-equivalent benchmarks."""
    def __init__(self, real, latency=0.0):
        self._real, self._latency = real, latency
        self.calls = []

    def table(self, name):
        return _BuilderProxy(self._real.table(name), name, self.calls, self._latency)

    def rpc(self, name, params=None):
        self.calls.append(("rpc", name))
        if self._latency:
            import time
            time.sleep(self._latency)
        return self._real.rpc(name, params or {})

    def count(self, table, op):
        return sum(1 for t, o in self.calls if t == table and o == op)

    def rpc_count(self, name):
        return sum(1 for t, o in self.calls if t == "rpc" and o == name)

    def total(self):
        return len(self.calls)
```
- [ ] **Step 4: Run, verify pass.** `uv run pytest tests/test_spy.py -v` → PASS.
- [ ] **Step 5: Commit.** `test: counting client proxy for round-trip regression`.

> NOTE for later tasks — round-trip regression tests (revised after review):
> - **Build ONE shared spy**, return it on every `get_client()`:
>   `spy = CountingClient(real); monkeypatch.setattr("crm.commands.<mod>.get_client", lambda: spy)`.
>   A fresh instance per call would give each worker thread its own counter.
> - **For dedup/backfill, prefer testing the inner function in ISOLATION**
>   (`_execute_cluster(spy, items, …)`, `_process_page(spy, rows, …)`) — NOT the
>   whole command. Reason: `dedup()`/`backfill()` also run `require_agent`,
>   `_load_pending`, and `build_plan` on the SAME client, and `build_plan` issues
>   `contact_identities` selects that collide with `_execute_cluster`'s on the same
>   (table, op) key — so a whole-command `count("contact_identities","select")`
>   assertion is polluted and WRONG. Isolate, or snapshot `len(spy.calls)` after
>   `build_plan` and assert the delta.
> - **Run threaded commands with `--workers 1`** in these tests so page/cluster
>   partitioning is deterministic (list.append is GIL-atomic so totals aren't lost,
>   but the *number of pages/retry-selects* varies with >1 worker).
> - **Assert N-invariance, not brittle exact constants**: e.g. dedup cluster K=2 vs
>   K=8 → identical call count (and `contacts.update` count == #distinct contacts,
>   independent of K); backfill 5 vs 50 rows/page → same RPC count; event 5 vs 50
>   participants → still 1 insert-batch + 1 bump RPC per chunk. This proves "no N+1"
>   and survives benign refactors. Use targeted `count()`/`rpc_count()`, not `total()`.

### Task 0.3: `src/crm/bulk.py` constants (kills the CHUNK import hazard)

**Files:** Create `src/crm/bulk.py`

- [ ] **Step 1:** Create `src/crm/bulk.py` with only a one-line docstring and
  `CHUNK = 500` and `PAGE = 1000` (module constants; `bulk.PAGE` is independent of
  `backfill.PAGE`). Nothing else yet — `_resolve_cohort`/gate are added in Task 2.1
  (which becomes "Modify", not "Create").
- [ ] **Step 2: Commit.** `feat(bulk): module constants (CHUNK, PAGE)`.

This makes `bulk.py` the single owner of `CHUNK` so Task 1.4's `from crm.bulk import CHUNK`
resolves cleanly and tests monkeypatch one constant.

Also in Task 0.1's coverage config, add `omit = ["tests/*", "scripts/*"]` under
`[tool.coverage.run]` (belt-and-suspenders; `source=["crm"]` already scopes it) and
drop the unused `from collections import Counter` in `tests/_spy.py`.

---

## Phase 1 — Performance fixes (lands migration 0006 + shared bump helper)

### Task 1.1: Migration 0006 — `bulk_upsert_interactions` + `crm_stats`

**Files:** Create `supabase/migrations/0006_perf_rpcs.sql`, `tests/test_perf_rpcs.py`

- [ ] **Step 1: Write behavioral tests (failing).** `tests/test_perf_rpcs.py` — model on `tests/test_dedup_rpcs.py`. The conftest fixture is named **`db`** and IS the supabase client (use `db.table(...)`/`db.rpc(...)`); the `rahul` agent is already seeded by migration 0001 and survives truncation, so no agent fixture is needed:
```python
def test_bulk_upsert_interactions_inserts_then_refreshes(db):
    cid = db.table("contacts").insert({"full_name": "A"}).execute().data[0]["id"]
    payload = [{"contact_id": cid, "event_id": None, "kind": "email",
                "channel": "irl", "occurred_at": "2026-01-01", "summary": "hi",
                "logged_by": "rahul", "source": "test", "source_external_id": "x1"}]
    db.rpc("bulk_upsert_interactions", {"payload": payload}).execute()
    rows = db.table("interactions").select("*").eq("source_external_id", "x1").execute().data
    assert len(rows) == 1 and rows[0]["summary"] == "hi"
    # refresh in place: mutate summary, keep kind/channel/logged_by
    payload[0]["summary"] = "updated"; payload[0]["kind"] = "call"  # kind must NOT change
    db.rpc("bulk_upsert_interactions", {"payload": payload}).execute()
    rows = db.table("interactions").select("*").eq("source_external_id", "x1").execute().data
    assert len(rows) == 1                      # no duplicate
    assert rows[0]["summary"] == "updated"     # mutable col refreshed
    assert rows[0]["kind"] == "email"          # immutable col preserved

def test_crm_stats_matches_legacy_counts(db):   # NOTE: fixture is `db`, no `client`
    # seed a known distribution touching every group-by source, leaving >=1 bucket
    # empty (exercises coalesce '{}'); compare crm_stats() flatten to hand counts.
    # Also assert a MIXED-empty case (one source table empty, others populated) and
    # that --json renders int 3 not 3.0.
    ...
```
Also add a `crm_stats` parity test asserting the flattened ordered list equals the legacy per-bucket counts on a seeded DB (dropped zero-bucket, kept `contacts_total`).
- [ ] **Step 2: Run, verify fail.** `supabase db reset && uv run pytest tests/test_perf_rpcs.py -v` → FAIL (functions missing).
- [ ] **Step 3: Write the migration.** `supabase/migrations/0006_perf_rpcs.sql`:
```sql
-- bulk_upsert_interactions: one statement insert-or-refresh against the partial
-- unique index interactions_source_ext (PostgREST .upsert() cannot target it).
-- Returns the PRIOR contact_ids of rows whose contact_id MOVED, so the caller can
-- recompute the abandoned contact's denorm (fixes denorm staleness on re-point).
create or replace function bulk_upsert_interactions(payload jsonb)
returns setof uuid
language sql
set search_path = public, extensions
as $$
  with prior as (
    -- capture existing (source_external_id -> contact_id) before the upsert
    select i.source_external_id, i.contact_id as old_cid
    from interactions i
    where i.source_external_id in (
      select p.source_external_id from jsonb_to_recordset(payload)
        as p(source_external_id text))
      and i.source_external_id is not null
  ),
  up as (
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
                  updated_at = now()
    returning source_external_id, contact_id as new_cid
  )
  select distinct prior.old_cid
  from up join prior using (source_external_id)
  where prior.old_cid is distinct from up.new_cid;
$$;
revoke execute on function bulk_upsert_interactions(jsonb) from public;
grant execute on function bulk_upsert_interactions(jsonb) to service_role;

-- bulk_bump_last_touchpoint: server-side guarded monotonic bump (no lost-update
-- race; mirrors bulk_add_tag's row-locked read-modify-write). Equal date = no-op.
create or replace function bulk_bump_last_touchpoint(
  p_ids uuid[], p_occurred date, p_channel text, p_topic text)
returns void
language sql
set search_path = public, extensions
as $$
  update contacts
  set last_touchpoint_at = p_occurred, last_touchpoint_channel = p_channel,
      last_touchpoint_topic = p_topic, updated_at = now()
  where id = any(p_ids)
    and (last_touchpoint_at is null or last_touchpoint_at < p_occurred);
$$;
revoke execute on function bulk_bump_last_touchpoint(uuid[], date, text, text) from public;
grant execute on function bulk_bump_last_touchpoint(uuid[], date, text, text) to service_role;

-- crm_stats: all coverage buckets in one round-trip. Counts cast to int so JSON
-- renders 3 not 3.0. Python re-imposes order + zero-bucket defaults.
create or replace function crm_stats()
returns jsonb
language sql
stable
set search_path = public, extensions
as $$
  select jsonb_build_object(
    'connection_status', (select coalesce(jsonb_object_agg(connection_status, c), '{}'::jsonb)
       from (select connection_status, count(*)::int c from contacts group by 1) s),
    'closeness_tier', (select coalesce(jsonb_object_agg(closeness_tier, c), '{}'::jsonb)
       from (select closeness_tier, count(*)::int c from contacts group by 1) s),
    'staging', (select coalesce(jsonb_object_agg(match_status, c), '{}'::jsonb)
       from (select match_status, count(*)::int c from staging group by 1) s),
    'touchpoints', (select coalesce(jsonb_object_agg(match_status, c), '{}'::jsonb)
       from (select match_status, count(*)::int c from staging_interactions group by 1) s),
    'contacts_total', (select count(*)::int from contacts)
  );
$$;
revoke execute on function crm_stats() from public;
grant execute on function crm_stats() to service_role;
-- ROLLBACK: drop function if exists bulk_upsert_interactions(jsonb);
--           drop function if exists bulk_bump_last_touchpoint(uuid[], date, text, text);
--           drop function if exists crm_stats();
```
Add behavioral tests for the new RPC behaviors: `bulk_upsert_interactions` returns
moved old contact_ids when a refresh re-points `contact_id` (and `[]` when none
move) and no-ops on empty `[]` payload; refresh flips `event_id` null→uuid;
`bulk_bump_last_touchpoint` bumps null/older, no-ops on equal/newer date, empty
`p_ids` no-op.
- [ ] **Step 4: Apply + run, verify pass.** `supabase db reset && uv run pytest tests/test_perf_rpcs.py -v` → PASS.
- [ ] **Step 5: Commit.** `feat(db): bulk_upsert_interactions + bulk_bump_last_touchpoint + crm_stats RPCs (0006)`.

### Task 1.2: Finding 2 — backfill uses `bulk_upsert_interactions`

**Files:** Modify `src/crm/commands/backfill.py` (`_process_page` ~82–144); tests in existing `tests/test_backfill*.py` + add a round-trip regression test.

- [ ] **Step 1: Write/extend tests (failing for the new contract).** Add to `tests/test_backfill.py`: a re-import (refresh) case asserting no duplicate interaction and `kind`/`channel`/`logged_by` unchanged; a round-trip regression test using the spy asserting one `bulk_upsert_interactions` rpc per page (not N updates). Assert orphan rows are still excluded from the payload and patched `orphaned`.
- [ ] **Step 2: Run, verify fail.**
- [ ] **Step 3: Refactor `_process_page`.** Remove the `existing` select-first map (98–104) and the per-row `interactions.update(...).eq("id", hit).execute()` (121–125). Build one `inserts` payload of all linked rows (each carrying the 9 columns from the RPC signature; `event_id` from `event_ids`; assert `source_external_id` not None), keep building the `patches` list. Replace the insert/update flush with:
```python
if inserts:
    moved = client.rpc("bulk_upsert_interactions", {"payload": inserts}).execute().data
    touched.update(r["bulk_upsert_interactions"] if isinstance(r, dict) else r
                   for r in (moved or []))   # union moved-away old contact_ids
```
(The RPC returns `setof uuid`; supabase-py yields a list of scalars or
single-key dicts depending on version — normalize to ids.) Keep orphan handling,
the `linked`/`orphaned` counters, and the staging upsert unchanged; ensure
`touched.add(contact_id)` still fires for **refreshed** rows (not just inserts),
and the moved-old-ids are unioned in so `_recompute` heals the abandoned contact.
- [ ] **Step 4: Run, verify pass.** `supabase db reset && uv run pytest tests/test_backfill.py tests/test_backfill_parallel.py -v`.
- [ ] **Step 5: Commit.** `perf(backfill): replace per-row refresh with bulk_upsert_interactions`.

### Task 1.3: Finding 5 — stats uses `crm_stats`

**Files:** Modify `src/crm/commands/admin.py` `stats` (74–98); add parity test in `tests/test_admin.py`.

- [ ] **Step 1: Write parity + round-trip test (failing).** Seed a known distribution; assert `stats` `--json` output equals the legacy ordered list; assert exactly one `crm_stats` rpc via the spy.
- [ ] **Step 2: Run, verify fail.**
- [ ] **Step 3: Reimplement `stats`.** Call `data = client.rpc("crm_stats", {}).execute().data`. Flatten by iterating the SAME fixed literal lists currently in admin.py (statuses, 5 tiers, 5 staging, 3 touchpoint), reading counts from the jsonb sub-objects defaulting missing keys to 0, then apply `if o["count"] or o["metric"] == "contacts_total"`. Preserve exact metric strings + order.
- [ ] **Step 4: Run, verify pass.** `uv run pytest tests/test_admin.py -v`.
- [ ] **Step 5: Commit.** `perf(stats): single crm_stats RPC replaces 16 head-counts`.

### Task 1.4: Finding 3 — event add batch + shared bulk bump helper

**Files:** Modify `src/crm/commands/log.py` (`_bump_last_touchpoint` 29–40, `log` 43–66, `event_add` 69–100); tests in `tests/test_log.py`.

- [ ] **Step 1: Write tests (failing).** Multi-participant `event add`: one `interactions.insert` (batch) + bump correctness; equal-date no-op; None-date event → no bump; empty `ids_to_bump` → no update call (spy). Single `log` still works (regression).
- [ ] **Step 2: Run, verify fail.**
- [ ] **Step 3: Add `_bump_last_touchpoint_bulk` (RPC-backed) + refactor.** In `log.py`
  (`CHUNK` is imported from `crm.bulk`, which exists from Task 0.3):
```python
from crm.bulk import CHUNK

def _bump_last_touchpoint_bulk(client, ids, occurred, channel, topic):
    # server-side guarded bump (no lost-update race; equal date = no-op)
    if not occurred or not ids:
        return
    for i in range(0, len(ids), CHUNK):
        client.rpc("bulk_bump_last_touchpoint", {
            "p_ids": ids[i:i + CHUNK], "p_occurred": occurred,
            "p_channel": channel, "p_topic": topic}).execute()
```
Refactor single `_bump_last_touchpoint` to call
`_bump_last_touchpoint_bulk(client, [contact_id], occurred, channel, topic)`.
Refactor `event_add`: resolve all refs (uuids batched via one `.in_("id", uuids)`,
names via `_resolve` keeping per-name errors; a uuid not returned by `.in_` must
raise `Exit(1)` like `_resolve` does for a missing name — don't silently drop;
resolve-before-any-write invariant preserved), then one `interactions.insert([...])`,
then one `_bump_last_touchpoint_bulk(client, ids, date, "irl", name)`.

Tests: equal-date no-op, None-date skip, multi-participant batch, unknown-uuid
participant → Exit(1), single `log` regression. (No empty-`ids_to_bump` client
branch any more — the guard is server-side.) This task now depends on migration
0006 (the bump RPC), which lands in Task 1.1.
- [ ] **Step 4: Run, verify pass.** `supabase db reset && uv run pytest tests/test_log.py -v`.
- [ ] **Step 5: Commit.** `perf(log): batch event-add inserts + shared bulk last-touchpoint bump`.

### Task 1.5: Finding 1 — dedup client-side batch fold

**Files:** Modify `src/crm/commands/dedup.py` (`_attach_identity` 36–51, `_fill_and_log` 54–73, `_execute_cluster` 94–135); tests in `tests/test_dedup*.py`.

Read perf spec Finding 1 in full — this task must preserve byte-identical routing.

**Recommended split (de-risks the byte-identical contract):** implement the fold
as a PURE function `_fold_auto_matched(items, contact_by_id, existing_identities)
-> (identity_inserts, enrichment_rows, per_contact_updates, outcomes)` first,
unit-tested with NO DB; then wire it into `_execute_cluster` (reads + flushes).

- [ ] **Step 1: Write behavior + regression tests (failing).** Pin both outcome patches by **key-absence**, not just status: attached → `auto_matched` carrying `match_confidence`; conflict → `needs_review`, `method='rerun_conflict'`, and assert `'resolved_at' not in patch` AND `'match_confidence' not in patch`. Cases:
  - two items filling the same null col on the same contact → earlier wins; later logs `import_conflict` with `old_value == <earlier item's value>` (NOT the pre-cluster DB value) — pins accumulator write-back;
  - same-contact existing identity → assert `contact_identities` count delta == 0 (no re-insert) AND fill+log still ran;
  - two staging rows in one cluster sharing the same `(source, source_external_id)` (distinct staging ids) → outcomes land on the correct rows (proves row-id keying); a same-cluster duplicate identity does NOT abort the batch (upsert ignore_duplicates);
  - a row auto-matching a sibling CREATED in the same cluster (accumulator seeded from the freshly-created contact's columns);
  - per-field fill vs conflict; `full_name` conflict.
  Round-trip regression — assert **N-invariance** (run the inner `_execute_cluster` in isolation, `--workers 1`): a single-contact cluster of K=2 and K=8 auto-matches issues the **same** number of calls, and `contacts.update` count == #distinct contacts (independent of K), `contact_identities` insert ≤ 1, `enrichment_log` insert ≤ 1. (Do NOT assert a brittle exact total.)
- [ ] **Step 2: Run, verify fail.**
- [ ] **Step 3: Reimplement the `auto_matched` branch of `_execute_cluster`.** ONLY this branch changes — `rejected`/`merged`/`needs_review` branches and their `_bump(state, lock, …)` calls stay byte-identical. Algorithm:
```
# after create RPC + keymap built:
auto = [it for it in items if it["match_status"] == "auto_matched"]
cids = sorted({deref(it["matched_ref"]) for it in auto})          # incl. created siblings
contact_by_id = {c["id"]: c for c in
    client.table("contacts").select("*").in_("id", cids).execute().data}  # 1 read (post-create)
existing = {}                                                     # (source,sxid) -> contact_id
for key in IDENTITY_KEY_COLS:  # the (source, source_external_id) pair; batch by .in_
    for row in client.table("contact_identities").select("source,source_external_id,contact_id")\
            .in_("source_external_id", [sxids...]).execute().data:
        existing[(row["source"], row["source_external_id"])] = row["contact_id"]
acc = {cid: dict(contact_by_id[cid]) for cid in cids}            # mutable per-contact state
seen_identities = dict(existing)                                  # combine DB + in-cluster queued
id_inserts, enrich_rows, fills = [], [], {}                      # fills: cid -> {col: val}
for it in auto:                                       # PLAN ORDER
    cid = deref(it["matched_ref"]); ident = it["identity"]; staged = it["staged"]
    k = (ident.get("source"), ident.get("source_external_id"))
    if ident.get("source_external_id") and k in seen_identities:
        if seen_identities[k] != cid:                # identity points elsewhere -> conflict
            outcomes[it["id"]] = "conflict"; continue
        # same contact: no re-insert, fall through to fill
    elif ident.get("source_external_id"):
        id_inserts.append({"contact_id": cid, **{f: ident.get(f) for f in IDENTITY_FIELDS}})
        seen_identities[k] = cid                      # so a later same-key item sees it
    # FILL against the ACCUMULATOR (mirrors per-item DB re-read):
    for cf, sf in FILL.items():
        new = staged.get(sf)
        if not new: continue
        if not acc[cid].get(cf):
            fills.setdefault(cid, {})[cf] = new; acc[cid][cf] = new   # write back!
        elif acc[cid][cf] != new:
            enrich_rows.append({"contact_id": cid, "field": cf, "old_value": acc[cid][cf],
                                "new_value": new, "source": it["source"], "method": "import_conflict"})
    if staged.get("full_name") and staged["full_name"] != acc[cid]["full_name"]:
        enrich_rows.append({...full_name conflict, old=acc[cid]["full_name"]...})
    outcomes[it["id"]] = "attached"
# FLUSH (each guarded, skip when empty):
if id_inserts: client.table("contact_identities").upsert(
        id_inserts, on_conflict="source,source_external_id", ignore_duplicates=True).execute()
if enrich_rows: client.table("enrichment_log").insert(enrich_rows).execute()
for cid, upd in fills.items():
    client.table("contacts").update({**upd, "updated_at": "now()"}).eq("id", cid).execute()
# build patches by outcome (auto_matched vs needs_review/rerun_conflict), keyed by it["id"]
```
  Key points an implementer MUST get right: (a) accumulator write-back (`acc[cid][cf]=new`) so later items see earlier fills; (b) `seen_identities` combines the prefetched DB map AND in-cluster queued inserts; (c) identity insert is `upsert(..., ignore_duplicates=True)` so a pre-existing/crash-leftover identity can't abort the batch; (d) one update per distinct contact that has fills (skip empties); (e) only the `auto_matched` branch changes. Delete the old per-item `_attach_identity`/`_fill_and_log` (or keep `_fold_auto_matched` as the pure helper).
- [ ] **Step 4: Run, verify pass.** `supabase db reset && uv run pytest tests/test_dedup.py tests/test_dedup_parallel.py tests/test_dedup_plan.py tests/test_dedup_rpcs.py -v`.
- [ ] **Step 5: Commit.** `perf(dedup): batch cluster reads/writes, keep routing in Python`.

### Task 1.6: Finding 4 — review queue two-pass

**Files:** Modify `src/crm/commands/dedup.py` `_candidate_display` (231–285), `review` (343–350); tests in `tests/test_review.py`.

- [ ] **Step 1: Write parity + regression test (failing).** Render parity vs current output for conflict + fuzzy rows — **compare multi-candidate conflict candidates as a SET/sorted, not byte-identical** (current `_candidate_display` builds candidates from a Python `set`, so order is already non-deterministic — capture the golden against current code first, compare order-insensitively). Role-email skip preserved; queue of R rows issues a small constant of reads, not O(R) — assert via N-invariance (R=2 vs R=10 → same read count).
- [ ] **Step 2: Run, verify fail.**
- [ ] **Step 3: Implement two-pass.** Pass 1: across the queue collect candidate contact_ids and identity `(field, value)` lookups (applying `_is_role_email` skip). One `contacts.in_("id", all_ids)` + one `contact_identities.in_()` per key column → maps. Make `_candidate_display(maps, q)` pure over the prefetched maps. Pass 2 renders from maps.
- [ ] **Step 4: Run, verify pass.** `uv run pytest tests/test_review.py -v`.
- [ ] **Step 5: Commit.** `perf(dedup): two-pass batched review queue rendering`.

### Task 1.7: Finding 6 — clustering bucket cap

**Files:** Modify `src/crm/clustering.py` (name-sim loop 87–96); Create `tests/test_clustering_cap.py`.

- [ ] **Step 1: Write tests (failing).** Oversized bucket (>200 sharing a common trigram) is skipped but true matches sharing a rare trigram still cluster; exact notice text emitted; negative branch — no notice when all buckets ≤ 200.
- [ ] **Step 2: Run, verify fail.**
- [ ] **Step 3: Implement.** Add `MAX_BUCKET = 200`. In the name-sim loop, skip buckets with `len(ids) > MAX_BUCKET`, count skips, and after the loop `err(f"clustering: skipped {n} oversized trigram bucket(s) (>200); rare-trigram edges still applied")` when `n`. Keep `tri_of` memo + union-find short-circuit.
- [ ] **Step 4: Run, verify pass.** `uv run pytest tests/test_clustering.py tests/test_clustering_cap.py -v`.
- [ ] **Step 5: Commit.** `perf(clustering): cap oversized trigram buckets`.

---

## Phase 2 — Bulk-edit commands (lands migration 0008)

### Task 2.1: `src/crm/bulk.py` — constants + `_resolve_cohort`

**Files:** Modify `src/crm/bulk.py` (created in Task 0.3); Modify `src/crm/commands/contacts.py` (`list_contacts` 76–104); Create `tests/test_bulk_cohort.py`.

- [ ] **Step 1: Write tests (failing).** Each filter maps correctly; AND composition; **distinct** ids; pagination past PAGE (monkeypatch `PAGE=2`, seed 3 → all returned, both loop branches). `list_contacts` output unchanged — assert columns, `nullsfirst` ordering, default limit=100 and cap=1000 byte-identical to current (characterization, captured on current code first).
- [ ] **Step 2: Run, verify fail.**
- [ ] **Step 3: Implement.** `bulk.py` already has `CHUNK`/`PAGE` (Task 0.3). Add a **filter-only** helper `_apply_filters(q, *, status, tier, tag, affiliation, cold_since)` that applies ONLY the five clauses (status→`.eq(connection_status)`, tier→`.eq(closeness_tier)`, tag→`.contains("tags",[tag])`, affiliation→`.contains("affiliations",[...])`, cold_since→the int-derived `.or_` cutoff using `date.today()`) to a passed-in builder and returns it — it must NOT touch `.select()`, `.order()`, `.limit()`, `.range()`. Then `_resolve_cohort(client, *, filters) -> list[str]` = `select("id")` → `_apply_filters` → `.range()` pagination loop until a short page → `sorted(set(ids))`. Refactor `list_contacts` to `select(<display cols>)` → `_apply_filters` → its own `.order(nullsfirst)/.limit`. `_emit`/`_gate` live in Task 2.2, NOT here.
- [ ] **Step 4: Run, verify pass.** `uv run pytest tests/test_bulk_cohort.py tests/test_contacts.py -v`.
- [ ] **Step 5: Commit.** `feat(bulk): _resolve_cohort + shared list filter helper`.

### Task 2.2: Cohort gate / dry-run / json / confirm helper

**Files:** Modify `src/crm/bulk.py`; tests in `tests/test_bulk_cohort.py`.

- [ ] **Step 1: Write tests (failing).** empty-filter-no-`--all` → exit 2; `--all`+filter → exit 2; empty cohort → no write/no RPC, exit 0, `changed_count:0`; `--dry-run` (count+sample with ids) and `--dry-run --json` (`{dry_run:true, cohort_count, affected}`); **write without `--yes` and without `--dry-run` → exit 2** ("pass --dry-run or --yes"); `--json` real shape (`{dry_run:false, cohort_count, affected, changed_count}`); dry-run ignores `--yes`/`--agent`; agent-not-registered → exit 1 (write path); dry-run with bogus agent → exit 0 (agent NOT validated); spy proves exactly ONE `agents` select across a multi-chunk write.
- [ ] **Step 2: Run, verify fail.**
- [ ] **Step 3: Implement the gate** (revised — NO interactive prompt). A helper `_gate(client, *, filters, all_, dry_run, yes, as_json, agent) -> list[ids] | STOP` that, in order: (1) validate filter/`--all` exclusivity → Exit(2); (2) if not `dry_run`: require `--yes` else Exit(2) ("pass --dry-run to preview or --yes to apply"), then `require_agent(client, agent)`; (3) `_resolve_cohort`; (4) if `dry_run`: emit preview (json `{dry_run:true,…}` / human count+sample-with-ids), return STOP; (5) if empty cohort: emit empty shape, return STOP; (6) return ids. `_emit(...)` produces the unified JSON. No `typer.confirm`, no `isatty()` — `--yes` is the sole write gate (matches the revised bulk spec).
- [ ] **Step 4: Run, verify pass.**
- [ ] **Step 5: Commit.** `feat(bulk): cohort gate (dry-run / --yes / json, agent-validated)`.

### Task 2.3: Migration 0008 — `bulk_add_tag`

**Files:** Create `supabase/migrations/0008_bulk_edit_rpcs.sql`, `tests/test_bulk_tag.py` (RPC part).

- [ ] **Step 1: Write behavioral test (failing).** Seed contacts (some already tagged); call RPC; assert idempotency (returns only newly-affected ids), sorted `tags` array, count = affected not cohort.
- [ ] **Step 2: Run, verify fail.** `supabase db reset && uv run pytest tests/test_bulk_tag.py -v`.
- [ ] **Step 3: Write migration.**
```sql
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
grant execute on function bulk_add_tag(text, uuid[]) to service_role;
-- ROLLBACK: drop function if exists bulk_add_tag(text, uuid[]);
```
Tests also: empty `p_ids` (`[]`) → returns `[]`, mutates nothing; a contact with
`tags = '{}'` (the default) gets `{p_tag}` (confirms the NOT-NULL default means no
coalesce needed).
- [ ] **Step 4: Apply + run, verify pass.** `supabase db reset && uv run pytest tests/test_bulk_tag.py -v`.
- [ ] **Step 5: Commit.** `feat(db): bulk_add_tag RPC (0008)`.

### Task 2.4: `crm bulk set` + register sub-app

**Files:** Create `src/crm/commands/bulk.py`; Modify `src/crm/cli.py`; Create `tests/test_bulk_set.py`.

- [ ] **Step 1: Write tests (failing).** no-`=` → exit 2; non-settable → exit 1; array field → exit 2; bad enum → exit 1; happy path updates all matched (chunked: monkeypatch `CHUNK=2`, 3 rows → per chunk: 1 read + 1 update + 1 enrichment insert, via spy); `enrichment_log` rows written with `method='bulk_set'` and **real per-row `old_value`** (captured from the pre-read); `--json` real shape with `affected`/`changed_count`.
- [ ] **Step 2: Run, verify fail.**
- [ ] **Step 3: Implement `bulk set` + register.** In `commands/bulk.py` create `bulk_app = typer.Typer(help=...)`; give each verb a `help=` string. `set` parses `field=value` (no-`=`→Exit 2), validates scalar-only + enum (per bulk spec), then calls `_gate(...)` (which does `--yes` check + `require_agent` + resolve; returns ids or STOP). Then per chunk over ids: read current `{id: field}` (one `.in_`), `update({field:value,"updated_at":"now()"}).in_("id", chunk)`, `enrichment_log.insert([...])` with captured `old_value`. Emit unified JSON (`changed`=cohort for set). In `cli.py` add `app.add_typer(bulk_app, name="bulk")`.
- [ ] **Step 4: Run, verify pass.** `uv run pytest tests/test_bulk_set.py tests/test_cli_smoke.py -v`.
- [ ] **Step 5: Commit.** `feat(bulk): crm bulk set`.

### Task 2.5: `crm bulk tag`

**Files:** Modify `src/crm/commands/bulk.py`; extend `tests/test_bulk_tag.py` (command part).

- [ ] **Step 1: Write tests (failing).** unknown tag → exit 1; happy path calls `bulk_add_tag` chunked (`CHUNK=2`); `--json` = `{dry_run:false, cohort_count:N, affected:[changed ids], changed_count:M}`; cohort where some already-tagged → `changed_count < cohort_count` (and human line "tagged M (N-M already had it)") — this is the count-clarity fix.
- [ ] **Step 2: Run, verify fail.**
- [ ] **Step 3: Implement.** `tag` registry-checks the tag (unknown → Exit 1), calls `_gate(...)` (does `--yes` + `require_agent` + resolve), then per chunk calls `bulk_add_tag(p_tag, chunk)`, accumulates returned ids as `affected` (the newly-tagged subset); emit unified JSON with `cohort_count` and `changed_count=len(affected)`.
- [ ] **Step 4: Run, verify pass.** `supabase db reset && uv run pytest tests/test_bulk_tag.py -v`.
- [ ] **Step 5: Commit.** `feat(bulk): crm bulk tag`.

### Task 2.6: `crm bulk log`

**Files:** Modify `src/crm/commands/bulk.py`; Create `tests/test_bulk_log.py`.

- [ ] **Step 1: Write tests (failing).** invalid kind → exit 1; invalid date → exit 1; happy path one `interactions.insert` per chunk (CHUNK=2, 3 rows → 2 inserts) + bump via `_bump_last_touchpoint_bulk` (equal-date/None handled server-side); `topic=summary`; `--json` real shape.
- [ ] **Step 2: Run, verify fail.**
- [ ] **Step 3: Implement.** `log` validates kind/date, calls `_gate(...)` (does `--yes`+`require_agent`+resolve), chunked `interactions.insert([...])`, then `_bump_last_touchpoint_bulk(client, ids, date, channel, topic=summary)`.
- [ ] **Step 4: Run, verify pass.** `uv run pytest tests/test_bulk_log.py -v`.
- [ ] **Step 5: Commit.** `feat(bulk): crm bulk log`.

---

## Phase 3 — Benchmark, docs, final verification

### Task 3.1: Benchmark harness

**Files:** Create `scripts/bench_bulk.py`

- [ ] **Step 1: Implement.** Seed (one bulk insert) so every refresh row is a `hit` and every participant is bump-eligible. For each scenario (event-add, backfill-refresh, stats, bulk verbs), run the NEW path vs a **reference naive per-row loop re-implemented inline in the script** (NOT in `crm/`; comment each with the old line range it mirrors — "naive baseline" for the net-new bulk verbs), each wrapped in `CountingClient`. **Headline = round-trip count ratios** (exact integers: 101→1, ~150→4, 16→1, N→⌈N/CHUNK⌉). Add ONE derived projection line per fix ("at 50ms RTT: ~Xs→~Ys, computed from counts") — do NOT report median/p90 of injected-sleep wall-clock (it's `count×sleep`, circular). Truncate data tables between runs (not full `db reset`); one warm-up discard.
- [ ] **Step 2: Run.** `supabase db reset && uv run python scripts/bench_bulk.py` → prints the round-trip ratio table + projection lines.
- [ ] **Step 3: Commit.** `bench: round-trip reduction harness`.

### Task 3.2: README

**Files:** Modify `README.md`

- [ ] **Step 1: Document `crm bulk set/tag/log`** in `README.md` "The loop", leading with `--dry-run`-first, the `--all`/filter requirement, `--yes` to apply, and `--json` for agents.
- [ ] **Step 2: Update `docs/operational-loads.md`** — the agent playbook — to mention the new cohort verbs (e.g. "after import/backfill, tag or bulk-log a cohort"). Add `coverage.xml` to `.gitignore`.
- [ ] **Step 3: Commit.** `docs: document crm bulk verbs + agent playbook`.

### Task 3.3: Full verification + ship

- [ ] **Step 1:** `supabase db reset && uv run pytest --cov=crm --cov-report=xml --cov-report=term-missing -rs -q` → **all green and `0 skipped`** (a skip means `.env.local`/stack misconfig — fix before trusting coverage; a silent skip would show as a phantom coverage miss).
- [ ] **Step 2:** `uv run diff-cover coverage.xml --compare-branch=main --fail-under=100` → 100% on changed Python lines. NOTE: diff-cover scores only files in `coverage.xml` (`src/crm/*.py`); `.sql` migrations, `tests/*`, `scripts/*` carry zero measured lines and are dropped — do NOT pragma them. Fix real gaps or add justified `# pragma: no cover` (budget 0–3, only genuinely-unreachable defensive limbs).
- [ ] **Step 3:** `uv run python scripts/bench_bulk.py` → capture the round-trip ratio table for the PR.
- [ ] **Step 4: Pre-publish gate (MANDATORY — repo is PUBLIC).** Invoke the `pre-publish` skill before any push: no secrets/PII/local-path leak (`.env.local` is gitignored — confirm it's not staged), migrations/specs clean.
- [ ] **Step 5:** Invoke `superpowers:requesting-code-review`, then `superpowers:finishing-a-development-branch`. The goal is "committed to main + GitHub" → choose the merge-to-`main` + push option (the skill offers options; pick the one that lands on main and pushes).

---

## Notes for the executor
- `src/crm/bulk.py` is created in **Task 0.3** with `CHUNK`/`PAGE`, so Task 1.4's `from crm.bulk import CHUNK` resolves and Task 2.1 only *modifies* it. No dangling import.
- Tasks 1.4 (single `log`/`event_add` use the bump RPC) and 1.2 depend on migration 0006 (Task 1.1) — run `supabase db reset` in those tasks too.
- Always `supabase db reset` before any task that adds/changes a migration or tests an RPC.
- The counting proxy (`tests/_spy.py`) is shared by every round-trip regression test and the benchmark — keep its interface stable. For dedup/backfill round-trip tests, exercise the inner function in isolation with `--workers 1` and assert N-invariance (see the Task 0.2 NOTE).
- This third revision (post 16-critic review) added: the `bulk_bump_last_touchpoint` RPC (lost-update fix), `bulk_upsert_interactions` returning moved contact_ids (denorm-staleness fix), `upsert(ignore_duplicates)` for dedup identity inserts, dropped the interactive prompt (`--yes` is the write gate), unified JSON shape, `old_value` capture in bulk set, `REVOKE … FROM PUBLIC`, the explicit Task 1.5 fold pseudocode, the pre-publish/merge finish steps, and benchmark-honesty.
