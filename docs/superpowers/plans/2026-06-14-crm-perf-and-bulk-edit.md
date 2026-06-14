# CRM Performance Fixes + Bulk-Edit Commands — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Eliminate six audited N+1 / perf issues in the `crm` CLI and add a `crm bulk set/tag/log` command family, with behavior preserved and 100% coverage on changed code.

**Architecture:** Replace per-row DB round-trips with (a) client-side read-batching + Python folds where subtle logic must be preserved, (b) one bulk `.update().in_()` where the value is uniform, and (c) plpgsql RPCs only where PostgREST can't express the op (partial-index upsert; GROUP BY aggregate; atomic array-append). Two new migrations: `0006_perf_rpcs.sql`, `0007_bulk_edit_rpcs.sql`.

**Tech Stack:** Python 3 · Typer · supabase-py (PostgREST) · Postgres (Supabase, local stack) · pytest · pytest-cov + diff-cover · uv.

**Source specs (read both before starting):**
- `docs/superpowers/specs/2026-06-14-crm-perf-fixes-design.md`
- `docs/superpowers/specs/2026-06-14-crm-bulk-edit-design.md`

**Global rules:**
- TDD: failing test → run-fail → implement → run-pass → commit. One logical change per commit.
- All tests run against the LOCAL Supabase stack only (`conftest.py` refuses non-local URLs).
- **Preflight before any DB test run:** `supabase db reset` to apply migrations (incl. new 0006/0007). `conftest.py` only truncates data tables; it does not create schema.
- plpgsql is invisible to coverage — every RPC branch is pinned by a **behavioral DB test** (seed → call → assert rows), following the existing `tests/test_dedup_rpcs.py` pattern.
- Commit message convention: end with `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`.

---

## File Structure

**Create:**
- `supabase/migrations/0006_perf_rpcs.sql` — `bulk_upsert_interactions`, `crm_stats`
- `supabase/migrations/0007_bulk_edit_rpcs.sql` — `bulk_add_tag`
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

> NOTE for later tasks: to spy a command, `monkeypatch.setattr("crm.commands.<mod>.get_client", lambda: CountingClient(real_client))`. For threaded commands (dedup/backfill) the workers call the same patched name, so all per-thread clients are counted.

---

## Phase 1 — Performance fixes (lands migration 0006 + shared bump helper)

### Task 1.1: Migration 0006 — `bulk_upsert_interactions` + `crm_stats`

**Files:** Create `supabase/migrations/0006_perf_rpcs.sql`, `tests/test_perf_rpcs.py`

- [ ] **Step 1: Write behavioral tests (failing).** `tests/test_perf_rpcs.py` — model on `tests/test_dedup_rpcs.py`. Seed a contact + agent, then:
```python
def test_bulk_upsert_interactions_inserts_then_refreshes(db, client, seed_agent):
    cid = client.table("contacts").insert({"full_name": "A"}).execute().data[0]["id"]
    payload = [{"contact_id": cid, "event_id": None, "kind": "email",
                "channel": "irl", "occurred_at": "2026-01-01", "summary": "hi",
                "logged_by": "rahul", "source": "test", "source_external_id": "x1"}]
    client.rpc("bulk_upsert_interactions", {"payload": payload}).execute()
    rows = client.table("interactions").select("*").eq("source_external_id", "x1").execute().data
    assert len(rows) == 1 and rows[0]["summary"] == "hi"
    # refresh in place: mutate summary, keep kind/channel/logged_by
    payload[0]["summary"] = "updated"; payload[0]["kind"] = "call"  # kind must NOT change
    client.rpc("bulk_upsert_interactions", {"payload": payload}).execute()
    rows = client.table("interactions").select("*").eq("source_external_id", "x1").execute().data
    assert len(rows) == 1                      # no duplicate
    assert rows[0]["summary"] == "updated"     # mutable col refreshed
    assert rows[0]["kind"] == "email"          # immutable col preserved

def test_crm_stats_matches_legacy_counts(db, client):
    # seed a known distribution, then compare crm_stats() output to hand counts
    ...
```
Also add a `crm_stats` parity test asserting the flattened ordered list equals the legacy per-bucket counts on a seeded DB (dropped zero-bucket, kept `contacts_total`).
- [ ] **Step 2: Run, verify fail.** `supabase db reset && uv run pytest tests/test_perf_rpcs.py -v` → FAIL (functions missing).
- [ ] **Step 3: Write the migration.** `supabase/migrations/0006_perf_rpcs.sql`:
```sql
-- bulk_upsert_interactions: one statement insert-or-refresh against the partial
-- unique index interactions_source_ext (PostgREST .upsert() cannot target it).
create or replace function bulk_upsert_interactions(payload jsonb)
returns void
language sql
set search_path = public, extensions
as $$
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
$$;
grant execute on function bulk_upsert_interactions(jsonb) to service_role;

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
grant execute on function crm_stats() to service_role;
-- ROLLBACK: drop function if exists bulk_upsert_interactions(jsonb);
--           drop function if exists crm_stats();
```
- [ ] **Step 4: Apply + run, verify pass.** `supabase db reset && uv run pytest tests/test_perf_rpcs.py -v` → PASS.
- [ ] **Step 5: Commit.** `feat(db): bulk_upsert_interactions + crm_stats RPCs (0006)`.

### Task 1.2: Finding 2 — backfill uses `bulk_upsert_interactions`

**Files:** Modify `src/crm/commands/backfill.py` (`_process_page` ~82–144); tests in existing `tests/test_backfill*.py` + add a round-trip regression test.

- [ ] **Step 1: Write/extend tests (failing for the new contract).** Add to `tests/test_backfill.py`: a re-import (refresh) case asserting no duplicate interaction and `kind`/`channel`/`logged_by` unchanged; a round-trip regression test using the spy asserting one `bulk_upsert_interactions` rpc per page (not N updates). Assert orphan rows are still excluded from the payload and patched `orphaned`.
- [ ] **Step 2: Run, verify fail.**
- [ ] **Step 3: Refactor `_process_page`.** Remove the `existing` select-first map (98–104) and the per-row `interactions.update(...).eq("id", hit).execute()` (121–125). Build one `inserts` payload of all linked rows (each carrying the 9 columns from the RPC signature; `event_id` from `event_ids`; assert `source_external_id` not None), keep building the `patches` list. Replace the insert/update flush with:
```python
if inserts:
    client.rpc("bulk_upsert_interactions", {"payload": inserts}).execute()
```
Keep orphan handling, `linked`/`orphaned` counters, and the staging upsert unchanged.
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
- [ ] **Step 3: Add `_bump_last_touchpoint_bulk` + refactor.** In `log.py`:
```python
from crm.bulk import CHUNK  # defined in Phase 2 Task 2.1; if 2.1 not yet landed,
                            # temporarily define CHUNK = 500 here and migrate later

def _bump_last_touchpoint_bulk(client, ids, occurred, channel, topic):
    if not occurred or not ids:
        return
    current = {}
    for i in range(0, len(ids), CHUNK):
        for r in (client.table("contacts").select("id,last_touchpoint_at")
                  .in_("id", ids[i:i + CHUNK]).execute().data):
            current[r["id"]] = r["last_touchpoint_at"]
    to_bump = [cid for cid in ids
               if not current.get(cid) or current[cid] < occurred]
    for i in range(0, len(to_bump), CHUNK):
        client.table("contacts").update(
            {"last_touchpoint_at": occurred, "last_touchpoint_channel": channel,
             "last_touchpoint_topic": topic, "updated_at": "now()"}
        ).in_("id", to_bump[i:i + CHUNK]).execute()
```
Refactor single `_bump_last_touchpoint` to call `_bump_last_touchpoint_bulk(client, [contact_id], occurred, channel, topic)`. Refactor `event_add`: resolve all refs (uuids via one `.in_`, names via `_resolve` keeping per-name errors), one `interactions.insert([...])`, then one `_bump_last_touchpoint_bulk(client, ids, date, "irl", name)`.

> **Ordering note:** to avoid a forward-import cycle, land `CHUNK` in `src/crm/bulk.py` (Task 2.1) FIRST, or define `CHUNK = 500` in `log.py` now and have `bulk.py` import it from there. Pick one in execution; the plan assumes `CHUNK` lives in `bulk.py` and `log.py` imports it — so do a tiny Phase-2-Task-2.1-lite (create `bulk.py` with just `CHUNK`/`PAGE`) before this task if sequencing strictly.
- [ ] **Step 4: Run, verify pass.** `uv run pytest tests/test_log.py -v`.
- [ ] **Step 5: Commit.** `perf(log): batch event-add inserts + shared bulk last-touchpoint bump`.

### Task 1.5: Finding 1 — dedup client-side batch fold

**Files:** Modify `src/crm/commands/dedup.py` (`_attach_identity` 36–51, `_fill_and_log` 54–73, `_execute_cluster` 94–135); tests in `tests/test_dedup*.py`.

Read perf spec Finding 1 in full — this task must preserve byte-identical routing.

- [ ] **Step 1: Write behavior + regression tests (failing).** Pin both outcome patches (attached → `auto_matched` with `match_confidence`; conflict → `needs_review`, `method='rerun_conflict'`, no `resolved_at`, no `match_confidence`). Cases: two items filling same null col on same contact (earlier wins, later logs `import_conflict` with `old_value=earlier`); same-contact existing identity (no re-insert, fill+log still run); a row auto-matching a sibling created in the same cluster; per-field fill vs conflict; `full_name` conflict. Round-trip regression: a K-item single-contact cluster issues 1 create RPC + 1 contacts read + 1 identities read + ≤1 identity insert + ≤1 enrichment insert + 1 contact update (not 3K).
- [ ] **Step 2: Run, verify fail.**
- [ ] **Step 3: Reimplement the auto-matched path in `_execute_cluster`.** Per perf spec: after the create RPC + `keymap`, collect distinct deref'd contact ids for auto-matched items; one `contacts.select("*").in_("id", ids)` → `contact_by_id`; one batched `contact_identities` read per identity key for the existing-identity/conflict check. Iterate items in plan order, maintaining a per-contact in-memory accumulator seeded from `contact_by_id`; apply the existing `_attach_identity` conflict rule (→ `conflict` outcome) and the `_fill_and_log` fill-null/`import_conflict`/`full_name` rules against the accumulator. Accumulate: identity inserts, enrichment_log rows, and per-contact final fill updates. Flush: one `contact_identities.insert([...])`, one `enrichment_log.insert([...])`, one `contacts.update(...).eq("id", cid)` per distinct contact. Key outcomes by staging row `id`; build `_patch(...)` per outcome exactly as today. Delete or inline `_attach_identity`/`_fill_and_log` as helpers operating on the accumulator (no per-item DB calls).
- [ ] **Step 4: Run, verify pass.** `supabase db reset && uv run pytest tests/test_dedup.py tests/test_dedup_parallel.py tests/test_dedup_plan.py tests/test_dedup_rpcs.py -v`.
- [ ] **Step 5: Commit.** `perf(dedup): batch cluster reads/writes, keep routing in Python`.

### Task 1.6: Finding 4 — review queue two-pass

**Files:** Modify `src/crm/commands/dedup.py` `_candidate_display` (231–285), `review` (343–350); tests in `tests/test_review.py`.

- [ ] **Step 1: Write parity + regression test (failing).** Render parity vs current output for conflict + fuzzy rows; role-email skip preserved; queue of R rows issues ~4 reads not O(R) (spy).
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

## Phase 2 — Bulk-edit commands (lands migration 0007)

### Task 2.1: `src/crm/bulk.py` — constants + `_resolve_cohort`

**Files:** Create `src/crm/bulk.py`; Modify `src/crm/commands/contacts.py` (`list_contacts` 76–104); Create `tests/test_bulk_cohort.py`.

- [ ] **Step 1: Write tests (failing).** Each filter maps correctly; AND composition; **distinct** ids; pagination past PAGE (monkeypatch `PAGE=2`, seed 3 → all returned, both loop branches). `list_contacts` output unchanged (regression vs current).
- [ ] **Step 2: Run, verify fail.**
- [ ] **Step 3: Implement `bulk.py`.** Define `CHUNK = 500`, `PAGE = 1000` (module constants). `_resolve_cohort(client, *, status, tier, tag, affiliation, cold_since) -> list[str]` builds the same query as `list_contacts` (status→connection_status, tier→closeness_tier, tag→.contains, affiliation→.contains, cold_since→or_ cutoff), pages with `.range(i, i+PAGE-1)` until a short page, returns `sorted(set(ids))`. Refactor `list_contacts` to build its display query from a shared filter-application helper (keep `--limit`/ordering). Also add `_emit(...)` json/human helper and a `_gate(...)` confirm/dry-run/all-validation helper (or split into Task 2.2).
- [ ] **Step 4: Run, verify pass.** `uv run pytest tests/test_bulk_cohort.py tests/test_contacts.py -v`.
- [ ] **Step 5: Commit.** `feat(bulk): _resolve_cohort + shared list filter helper`.

### Task 2.2: Cohort gate / dry-run / json / confirm helper

**Files:** Modify `src/crm/bulk.py`; tests in `tests/test_bulk_cohort.py`.

- [ ] **Step 1: Write tests (failing).** empty-filter-no-`--all` → exit 2; `--all`+filter → exit 2; empty cohort → no write, exit 0, `count:0`, no prompt; `--dry-run` (count+sample) and `--dry-run --json` (`would_affect`); non-TTY-no-flags → exit 2; TTY confirm y/N (mock `typer.confirm`); `--json` (`affected`); dry-run ignores `--yes`/`--agent`.
- [ ] **Step 2: Run, verify fail.**
- [ ] **Step 3: Implement the gate.** A helper that: validates filter/`--all` exclusivity; resolves the cohort; on `--dry-run` emits preview (json `would_affect` / human) and returns a sentinel "stop"; on empty cohort emits `count:0` and returns "stop"; else handles the confirm gate (TTY prompt unless `--yes`/`--json`; non-TTY without flags → `typer.Exit(2)`); returns the resolved ids for the verb to write. Agent validation (`require_agent`) is called by each verb **before** the gate's write path but skipped on dry-run.
- [ ] **Step 4: Run, verify pass.**
- [ ] **Step 5: Commit.** `feat(bulk): dry-run/confirm/json cohort gate`.

### Task 2.3: Migration 0007 — `bulk_add_tag`

**Files:** Create `supabase/migrations/0007_bulk_edit_rpcs.sql`, `tests/test_bulk_tag.py` (RPC part).

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
grant execute on function bulk_add_tag(text, uuid[]) to service_role;
-- ROLLBACK: drop function if exists bulk_add_tag(text, uuid[]);
```
- [ ] **Step 4: Apply + run, verify pass.** `supabase db reset && uv run pytest tests/test_bulk_tag.py -v`.
- [ ] **Step 5: Commit.** `feat(db): bulk_add_tag RPC (0007)`.

### Task 2.4: `crm bulk set` + register sub-app

**Files:** Create `src/crm/commands/bulk.py`; Modify `src/crm/cli.py`; Create `tests/test_bulk_set.py`.

- [ ] **Step 1: Write tests (failing).** no-`=` → exit 2; non-settable → exit 1; array field → exit 2; bad enum → exit 1; happy path updates all matched (chunked: monkeypatch `CHUNK=2`, 3 rows → 2 update calls + 2 enrichment inserts via spy); `enrichment_log` rows written (`method='bulk_set'`); `--json` affected ids.
- [ ] **Step 2: Run, verify fail.**
- [ ] **Step 3: Implement `bulk set` + register.** In `commands/bulk.py` create `bulk_app = typer.Typer(...)`. `set` parses `field=value`, validates per bulk spec (scalar-only, enum), runs the gate, then `require_agent`, then chunked `contacts.update({field:value,...}).in_("id", chunk)` + chunked `enrichment_log.insert([...])`. In `cli.py` add `app.add_typer(bulk_app, name="bulk")`.
- [ ] **Step 4: Run, verify pass.** `uv run pytest tests/test_bulk_set.py tests/test_cli_smoke.py -v`.
- [ ] **Step 5: Commit.** `feat(bulk): crm bulk set`.

### Task 2.5: `crm bulk tag`

**Files:** Modify `src/crm/commands/bulk.py`; extend `tests/test_bulk_tag.py` (command part).

- [ ] **Step 1: Write tests (failing).** unknown tag → exit 1; happy path calls `bulk_add_tag` chunked (`CHUNK=2`); `--json` affected = returned ids; cohort with some already-tagged reports affected count.
- [ ] **Step 2: Run, verify fail.**
- [ ] **Step 3: Implement.** `tag` registry-checks the tag, runs gate + `require_agent`, calls `bulk_add_tag(p_tag, chunk)` per chunk, accumulates returned ids for the count/json.
- [ ] **Step 4: Run, verify pass.** `supabase db reset && uv run pytest tests/test_bulk_tag.py -v`.
- [ ] **Step 5: Commit.** `feat(bulk): crm bulk tag`.

### Task 2.6: `crm bulk log`

**Files:** Modify `src/crm/commands/bulk.py`; Create `tests/test_bulk_log.py`.

- [ ] **Step 1: Write tests (failing).** invalid kind → exit 1; invalid date → exit 1; happy path one `interactions.insert` per chunk + bump via `_bump_last_touchpoint_bulk` (equal-date/None inherited); `topic=summary`; `--json` affected.
- [ ] **Step 2: Run, verify fail.**
- [ ] **Step 3: Implement.** `log` validates kind/date, runs gate + `require_agent`, chunked `interactions.insert([...])`, then `_bump_last_touchpoint_bulk(client, ids, date, channel, topic=summary)`.
- [ ] **Step 4: Run, verify pass.** `uv run pytest tests/test_bulk_log.py -v`.
- [ ] **Step 5: Commit.** `feat(bulk): crm bulk log`.

---

## Phase 3 — Benchmark, docs, final verification

### Task 3.1: Benchmark harness

**Files:** Create `scripts/bench_bulk.py`

- [ ] **Step 1: Implement.** Seed N ∈ {100, 1000} contacts (one bulk insert). For each scenario (event-add, backfill-refresh, stats, bulk verbs), run the NEW path vs a **reference naive per-row loop re-implemented in the script** (not in `crm/`), each wrapped in a `CountingClient` — once with `latency=0` (report round-trip counts) and once with `latency=0.05` (report median+p90 wall-clock over ≥5 reps via `perf_counter`, labeled "remote-equivalent 50ms RTT"). Reset DB state between runs.
- [ ] **Step 2: Run.** `supabase db reset && uv run python scripts/bench_bulk.py` → prints a table of round-trip reductions + remote-equivalent timings.
- [ ] **Step 3: Commit.** `bench: round-trip + injected-latency harness`.

### Task 3.2: README

**Files:** Modify `README.md`

- [ ] **Step 1: Document `crm bulk set/tag/log`** in "The loop", leading with the `--dry-run`-first workflow and noting the `--all`/filter requirement and `--json` for agents.
- [ ] **Step 2: Commit.** `docs: document crm bulk verbs`.

### Task 3.3: Full verification

- [ ] **Step 1:** `supabase db reset && uv run pytest --cov=crm --cov-report=xml --cov-report=term-missing -q` → all green.
- [ ] **Step 2:** `uv run diff-cover coverage.xml --compare-branch=main --fail-under=100` → 100% on changed lines. Fix gaps or add justified `# pragma: no cover`.
- [ ] **Step 3:** `uv run python scripts/bench_bulk.py` → capture the numbers for the PR.
- [ ] **Step 4:** Invoke `superpowers:requesting-code-review`, then `superpowers:finishing-a-development-branch` to integrate.

---

## Notes for the executor
- If strict task ordering causes the `log.py`↔`bulk.py` `CHUNK` import to dangle, do a 2-line pre-task: create `src/crm/bulk.py` with just `CHUNK = 500` / `PAGE = 1000` before Task 1.4, then flesh it out in Task 2.1.
- Always `supabase db reset` before any task that adds/changes a migration or tests an RPC.
- The counting proxy (`tests/_spy.py`) is shared by every round-trip regression test and the benchmark — keep its interface stable.
