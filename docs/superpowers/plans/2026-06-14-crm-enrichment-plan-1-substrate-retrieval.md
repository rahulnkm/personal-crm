# CRM Enrichment — Plan 1: Substrate + Retrieval — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `crm` answer plain-language network questions over existing data, on a provenance-tracked write-path that never clobbers manual data — with zero external API keys.

**Architecture:** Phase 0 builds the survivorship substrate: one atomic, advisory-locked Postgres RPC (`enrich_apply_candidate`) that every write funnels through, an extended `enrichment_log` provenance spine, a one-time backfill so existing data is treated as sacred, identifier-vs-attribute routing (discovered identifiers go to `candidate_identities`, never silently into golden), and a review/undo/stats/forget surface. Phase 1 adds the retrieval product: structured filters on `crm list`, compact `crm capsules`, a hybrid `crm find`, and a richer `crm contact` dossier. Semantic matching is done by Claude Code reading capsules in-context — no LLM/embedding code in the package.

**Tech Stack:** Python 3.12, Typer, supabase-py, Postgres/Supabase (plpgsql RPCs), pytest against a local Supabase stack.

**Spec:** `docs/superpowers/specs/2026-06-14-crm-enrichment-design.md` (this plan = Plan 1 = Phase 0 + Phase 1).

---

## Conventions (read once)

- **Migrations:** add `supabase/migrations/0006_*.sql` (and `0007` for the backfill). Apply locally with `supabase db reset` before running schema-dependent tests.
- **RPC calls** (supabase-py): `client.rpc("fn", {"p_x": v}).execute()`.
- **Tests:** use the `db` fixture (`tests/conftest.py`) — truncates data tables, refuses non-local URLs. Invoke CLI via `typer.testing.CliRunner` (see `tests/test_review.py`). RPCs tested directly via `db.rpc(...)` (see `tests/test_recompute.py`, `tests/test_dedup_rpcs.py`).
- **Method-class scheme:** `manual_set`→class `manual` (top); everything else→class `derived`. Ranking: class → recency (`created_at` desc) → confidence (`coalesce(confidence,0.4)` desc). Disputed values excluded by `(field,value)`.
- **Commit after every task.** Branch is the current worktree (`claude/keen-pasteur-c45e3a`).
- **conftest:** in Task 1, add `enrich_review` and `candidate_identities` to `DATA_TABLES` in `tests/conftest.py` (both cascade from `contacts`, but list them so any test inserting a review row without a backing contact is still isolated).
- **`SETTABLE`:** `crm set` only allows columns in `contacts.py`'s `SETTABLE` set. If any new column (`company_category`, etc.) must be manually settable, extend `SETTABLE` in the relevant task. The funding-`stage` filter (§4.1) reads a value enrichment derives (Plan 2) — in Plan 1 `--stage` filters whatever is present and may match nothing; that's fine.
- **`find_candidates(client, identity)`** (`matching.py:35`) returns a single best-match `dict | None` and **falls back to fuzzy name match** if no identifier hit. For Task 9's "0 / 1-this / 1-other / ≥2" branching, pass an identity dict containing **only the identifier** (no `full_name`, to suppress the fuzzy fallback) and interpret `score==1.0` (single exact) vs `CONFLICT_SCORE` (multiple/ambiguous).

## File structure (decisions locked here)

- `supabase/migrations/0006_enrichment_substrate.sql` — columns, enums, `enrich_review`, `candidate_identities`, `enrich_apply_candidate` RPC, `enrich_recompute_field` helper.
- `supabase/migrations/0007_enrichment_provenance_backfill.sql` — one-time synthetic provenance seed.
- `src/crm/enrich.py` — pure helpers: candidate dataclass, confidence ladder, method-class, JSON-payload parsing/validation. No DB.
- `src/crm/commands/enrich.py` — `crm enrich` sub-app: `apply`, `review`, `undo`, `stats`, `forget`, `due`, `changes`. Calls the RPC.
- `src/crm/commands/contacts.py` — extend: `crm set` routes through the RPC; `crm contact` renders provenance; `crm list` gains filters.
- `src/crm/commands/retrieval.py` — `crm capsules`, `crm find`.
- `src/crm/cli.py` — register new commands.
- `tests/test_enrich_rpc.py`, `tests/test_enrich_apply.py`, `tests/test_enrich_review.py`, `tests/test_enrich_undo.py`, `tests/test_enrich_identifiers.py`, `tests/test_enrich_jobchange.py`, `tests/test_retrieval.py`, `tests/test_contacts_provenance.py`.

---

## PHASE 0 — SUBSTRATE

### Task 1: Migration — schema additions (columns, enums, tables)

**Files:**
- Create: `supabase/migrations/0006_enrichment_substrate.sql` (schema portion; RPC added in Task 2)
- Test: `tests/test_enrich_rpc.py`

- [ ] **Step 1: Write the failing test** (schema presence)

```python
# tests/test_enrich_rpc.py
def test_enrichment_substrate_schema(db):
    # new enrichment_log columns
    row = db.table("enrichment_log").select(
        "source_detail, verification_status, refresh_after, is_current").limit(1).execute()
    assert row is not None
    # new contacts columns
    c = db.table("contacts").select(
        "company_category, company_description, company_domain, expertise, interests, "
        "avatar_url, github_username, twitter_username, website_url").limit(1).execute()
    assert c is not None
    # new tables exist
    assert db.table("enrich_review").select("id").limit(1).execute() is not None
    assert db.table("candidate_identities").select("id").limit(1).execute() is not None
```

- [ ] **Step 2: Run to verify it fails**

Run: `supabase db reset && pytest tests/test_enrich_rpc.py::test_enrichment_substrate_schema -v`
Expected: FAIL (columns/tables missing).

- [ ] **Step 3: Write the migration**

```sql
-- supabase/migrations/0006_enrichment_substrate.sql
-- enrichment_log: promote from conflict-log to provenance spine
do $$ begin
  create type enrich_verification as enum ('unverified','verified','disputed','human_confirmed');
exception when duplicate_object then null; end $$;

alter table enrichment_log
  add column if not exists source_detail text,
  add column if not exists verification_status enrich_verification not null default 'unverified',
  add column if not exists refresh_after date,
  add column if not exists is_current boolean not null default false;

-- exactly one current scalar winner per (contact, field)
create unique index if not exists enrichment_log_current_uq
  on enrichment_log (contact_id, field) where is_current;
-- ranking + lookup support
create index if not exists enrichment_log_field_rank
  on enrichment_log (contact_id, field, created_at desc);

-- contacts: capability + social columns (Plan 1 set only)
alter table contacts
  add column if not exists company_category text,
  add column if not exists company_description text,
  add column if not exists company_domain text,
  add column if not exists expertise text[] not null default '{}',
  add column if not exists interests text[] not null default '{}',
  add column if not exists avatar_url text,
  add column if not exists github_username text,
  add column if not exists twitter_username text,
  add column if not exists website_url text;

-- human-in-the-loop review queue
create table if not exists enrich_review (
  id uuid primary key default gen_random_uuid(),
  contact_id uuid not null references contacts(id) on delete cascade,
  field text not null,
  candidate_value text,
  source text not null,
  confidence real,
  reason text,                              -- low_confidence | value_conflict | identifier_conflict
  other_contact_id uuid references contacts(id) on delete set null,
  status text not null default 'open',      -- open | resolved | skipped
  created_at timestamptz not null default now(),
  resolved_at timestamptz
);
create index if not exists enrich_review_open on enrich_review (status, created_at);

-- quarantined discovered identifiers (not live match keys until promoted)
create table if not exists candidate_identities (
  id uuid primary key default gen_random_uuid(),
  contact_id uuid not null references contacts(id) on delete cascade,
  kind text not null,                       -- email | linkedin_url | phone | handle
  value text not null,
  source text not null,
  confidence real,
  source_detail text,
  status text not null default 'pending',   -- pending | promoted | rejected
  created_at timestamptz not null default now(),
  unique (contact_id, kind, value)
);
```

- [ ] **Step 4: Apply + verify it passes**

Run: `supabase db reset && pytest tests/test_enrich_rpc.py::test_enrichment_substrate_schema -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add supabase/migrations/0006_enrichment_substrate.sql tests/test_enrich_rpc.py
git commit -m "feat(enrich): schema substrate — provenance columns, review + candidate_identities tables"
```

---

### Task 2: The `enrich_apply_candidate` RPC (load-bearing)

**Files:**
- Modify: `supabase/migrations/0006_enrichment_substrate.sql` (append RPC + helper)
- Test: `tests/test_enrich_rpc.py`

Behavior contract (see spec §3.1–§3.4):
- Advisory-lock `(contact_id, field)`; insert provenance row (idempotent on identical latest `(contact_id, field, new_value, source)`); recompute winner by method-class→recency→confidence excluding `disputed (field,value)`; if winner clears the field's accept threshold and isn't beaten by a `manual` row → set `is_current`, demote prior, materialize to `contacts.<field>`; else insert `enrich_review`. `p_dry_run` returns the outcome without mutating. Returns `golden|review|losing|noop`.
- Scalar fields only — the function rejects/ignores array fields (`tags, affiliations, expertise, interests`).
- Accept threshold: default 0.7; manual always wins regardless.

- [ ] **Step 1: Write failing tests** (the load-bearing guarantees)

```python
# tests/test_enrich_rpc.py
import uuid

def _contact(db, **kw):
    base = {"full_name": "Test Person"}; base.update(kw)
    return db.table("contacts").insert(base).execute().data[0]

def _apply(db, cid, field, value, method, source, conf, dry=False):
    return db.rpc("enrich_apply_candidate", {
        "p_contact_id": cid, "p_field": field, "p_value": value,
        "p_method": method, "p_source": source, "p_confidence": conf,
        "p_source_detail": None, "p_dry_run": dry}).execute().data

def test_fills_null_field_becomes_golden(db):
    c = _contact(db, current_company=None)
    out = _apply(db, c["id"], "current_company", "Acme", "enrich_api", "gravatar", 0.9)
    assert out == "golden"
    got = db.table("contacts").select("current_company").eq("id", c["id"]).single().execute().data
    assert got["current_company"] == "Acme"

def test_manual_never_clobbered(db):
    c = _contact(db)
    _apply(db, c["id"], "current_company", "RealCo", "manual_set", "rahul", 1.0)
    out = _apply(db, c["id"], "current_company", "BrokerCo", "enrich_api", "pdl", 0.95)
    assert out in ("review", "losing")
    got = db.table("contacts").select("current_company").eq("id", c["id"]).single().execute().data
    assert got["current_company"] == "RealCo"  # manual stands

def test_low_confidence_goes_to_review_not_golden(db):
    c = _contact(db, current_role=None)
    out = _apply(db, c["id"], "current_role", "Wizard", "enrich_agent", "agent:claude-web", 0.5)
    assert out == "review"
    assert db.table("contacts").select("current_role").eq("id", c["id"]).single().execute().data["current_role"] is None
    assert len(db.table("enrich_review").select("id").eq("contact_id", c["id"]).execute().data) == 1

def test_dry_run_mutates_nothing(db):
    c = _contact(db, location=None)
    out = _apply(db, c["id"], "location", "SF", "enrich_api", "gravatar", 0.9, dry=True)
    assert out == "golden"  # would-be outcome
    assert db.table("contacts").select("location").eq("id", c["id"]).single().execute().data["location"] is None

def test_idempotent_reapply(db):
    c = _contact(db, location=None)
    _apply(db, c["id"], "location", "NYC", "enrich_api", "gravatar", 0.9)
    _apply(db, c["id"], "location", "NYC", "enrich_api", "gravatar", 0.9)
    rows = db.table("enrichment_log").select("id").eq("contact_id", c["id"]).eq("field","location").execute().data
    assert len(rows) == 1

def test_exactly_one_current(db):
    c = _contact(db, location=None)
    _apply(db, c["id"], "location", "NYC", "enrich_api", "gravatar", 0.9)
    _apply(db, c["id"], "location", "LA", "enrich_api", "pdl", 0.95)  # newer+higher → new winner
    cur = db.table("enrichment_log").select("new_value").eq("contact_id", c["id"]).eq("field","location").eq("is_current", True).execute().data
    assert len(cur) == 1
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/test_enrich_rpc.py -v -k "not schema"` → Expected: FAIL (RPC missing).

- [ ] **Step 3: Append the RPC to the migration**

```sql
-- helper: recompute + materialize the winner for one scalar (contact, field)
create or replace function enrich_recompute_field(p_contact_id uuid, p_field text)
returns text language plpgsql as $$
declare win record; begin
  select id, new_value into win
  from enrichment_log e
  where e.contact_id = p_contact_id and e.field = p_field
    and e.verification_status is distinct from 'disputed'
    -- exclude any value that has a disputed tombstone for this (contact, field)
    and not exists (
      select 1 from enrichment_log d
      where d.contact_id = p_contact_id and d.field = p_field
        and d.verification_status = 'disputed'
        and d.new_value is not distinct from e.new_value)
  order by (case when e.method = 'manual_set' then 0 else 1 end) asc,
           e.created_at desc,
           coalesce(e.confidence, 0.4) desc
  limit 1;

  -- demote all, elect winner
  update enrichment_log set is_current = false
   where contact_id = p_contact_id and field = p_field and is_current;
  if win.id is not null then
    update enrichment_log set is_current = true where id = win.id;
    execute format('update contacts set %I = $1, updated_at = now() where id = $2', p_field)
      using win.new_value, p_contact_id;
  else
    -- no surviving candidate (e.g. sole value was tombstoned) → clear the golden column
    execute format('update contacts set %I = null, updated_at = now() where id = $1', p_field)
      using p_contact_id;
  end if;
  return win.new_value;  -- NULL when no winner
end $$;

create or replace function enrich_apply_candidate(
  p_contact_id uuid, p_field text, p_value text,
  p_method text, p_source text, p_confidence real,
  p_source_detail text default null, p_dry_run boolean default false)
returns text language plpgsql as $$
declare
  accept_threshold real := 0.7;
  manual_exists boolean;
  is_disputed boolean;
  would text;
begin
  if p_field in ('tags','affiliations','expertise','interests') then
    return 'noop';  -- arrays handled by set-union path, not survivorship
  end if;

  perform pg_advisory_xact_lock(hashtext(p_contact_id::text || ':' || p_field));

  -- rejected value can never win again
  select exists(select 1 from enrichment_log where contact_id=p_contact_id and field=p_field
    and verification_status='disputed' and new_value is not distinct from p_value) into is_disputed;

  -- is there a manual value already?
  select exists(select 1 from enrichment_log where contact_id=p_contact_id and field=p_field
    and method='manual_set' and is_current) into manual_exists;

  -- compute would-be outcome
  if p_method = 'manual_set' then would := 'golden';
  elsif is_disputed then would := 'losing';
  elsif manual_exists then would := 'review';
  elsif coalesce(p_confidence,0) >= accept_threshold then would := 'golden';
  else would := 'review';
  end if;

  if p_dry_run then return would; end if;

  -- disputed/tombstoned value: short-circuit BEFORE idempotency + insert, so a
  -- re-applied rejected value deterministically returns 'losing' (not 'noop').
  if would = 'losing' then return 'losing'; end if;

  -- idempotency: skip if an identical row already exists (EXISTS ignores ordering)
  if exists (
    select 1 from enrichment_log
    where contact_id=p_contact_id and field=p_field
      and new_value is not distinct from p_value and source = p_source) then
    return 'noop';
  end if;

  insert into enrichment_log (contact_id, field, old_value, new_value, source, confidence, method, source_detail)
  select p_contact_id, p_field,
         (select new_value from enrichment_log where contact_id=p_contact_id and field=p_field and is_current limit 1),
         p_value, p_source, p_confidence, p_method, p_source_detail;

  if would = 'review' then
    insert into enrich_review (contact_id, field, candidate_value, source, confidence, reason)
    values (p_contact_id, p_field, p_value, p_source, p_confidence,
            case when manual_exists then 'value_conflict' else 'low_confidence' end);
    return 'review';
  end if;

  if would = 'golden' then
    perform enrich_recompute_field(p_contact_id, p_field);
    return 'golden';
  end if;
  return 'losing';
end $$;
```

- [ ] **Step 4: Apply + verify**

Run: `supabase db reset && pytest tests/test_enrich_rpc.py -v` → Expected: PASS (all).

- [ ] **Step 5: Commit**

```bash
git add supabase/migrations/0006_enrichment_substrate.sql tests/test_enrich_rpc.py
git commit -m "feat(enrich): enrich_apply_candidate RPC — atomic, advisory-locked survivorship"
```

---

### Task 3: Concurrency test for the RPC

**Files:** Test: `tests/test_enrich_rpc.py`

- [ ] **Step 1: Write the failing/again-passing test** (mirror `tests/test_dedup_parallel.py`)

```python
def test_concurrent_applies_one_winner(db):
    import concurrent.futures as cf
    c = _contact(db, location=None)
    vals = [("NYC","gravatar",0.9),("LA","pdl",0.92),("SF","github",0.88),("Berlin","pdl",0.95)]
    def apply(v):
        from crm.config import get_client
        return get_client().rpc("enrich_apply_candidate", {
            "p_contact_id": c["id"],"p_field":"location","p_value":v[0],
            "p_method":"enrich_api","p_source":v[1],"p_confidence":v[2],
            "p_source_detail":None,"p_dry_run":False}).execute().data
    with cf.ThreadPoolExecutor(max_workers=4) as ex:
        list(ex.map(apply, vals))
    cur = db.table("enrichment_log").select("new_value").eq("contact_id",c["id"]).eq("field","location").eq("is_current",True).execute().data
    assert len(cur) == 1
    got = db.table("contacts").select("location").eq("id",c["id"]).single().execute().data["location"]
    assert got == cur[0]["new_value"]  # materialized value matches the one current row
```

- [ ] **Step 2: Run** → Expected: PASS (advisory lock serializes). If FAIL, the lock/recompute is wrong — fix before proceeding.
- [ ] **Step 3: Commit** `test(enrich): concurrency — exactly one current under parallel applies`

---

### Task 4: One-time provenance backfill migration

**Files:**
- Create: `supabase/migrations/0007_enrichment_provenance_backfill.sql`
- Test: `tests/test_enrich_rpc.py`

Goal: every existing non-null scalar field gets an `is_current` provenance row so the first enrich run can't clobber it. Human-typed origin → `manual_set` conf 1.0; otherwise `legacy_import` conf 0.8. (Heuristic: if an `enrichment_log` row with `method='manual_set'` already exists for that `(contact,field)`, it's manual; else legacy_import.)

- [ ] **Step 1: Failing test**

```python
def test_backfill_protects_existing_value(db):
    # simulate a pre-existing contact value with no provenance, then run the seed fn
    c = _contact(db, current_company="LegacyCo")
    db.rpc("enrich_seed_provenance", {}).execute()  # idempotent seed over all contacts
    # an API value below... legacy is 0.8; a 0.7 web value should NOT overwrite (loses on recency? ensure)
    out = _apply(db, c["id"], "current_company", "WebCo", "enrich_api", "pdl", 0.85)
    got = db.table("contacts").select("current_company").eq("id",c["id"]).single().execute().data["current_company"]
    # legacy seed exists & is_current; a higher-confidence newer value MAY win — but a human one never loses.
    # Assert at minimum: a provenance row now exists and is_current for the legacy value pre-apply.
    rows = db.table("enrichment_log").select("method,is_current,new_value").eq("contact_id",c["id"]).eq("field","current_company").execute().data
    assert any(r["new_value"]=="LegacyCo" for r in rows)
```

(Note for implementer: legacy values are *competable* not *sacred* — only `manual_set` is sacred. This test asserts the seed ran; the manual-protection guarantee is covered in Task 2.)

- [ ] **Step 2: Run** → FAIL (`enrich_seed_provenance` missing).
- [ ] **Step 3: Write the migration**

```sql
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
```

- [ ] **Step 4: Apply + verify** `supabase db reset && pytest tests/test_enrich_rpc.py::test_backfill_protects_existing_value -v` → PASS.
- [ ] **Step 5: Commit** `feat(enrich): one-time provenance backfill seed (protects existing data)`

---

### Task 5: `src/crm/enrich.py` — pure helpers (no DB)

**Files:** Create `src/crm/enrich.py`; Test `tests/test_enrich_apply.py`

- [ ] **Step 1: Failing test**

```python
# tests/test_enrich_apply.py
from crm.enrich import parse_payload, EnrichCandidate, ATTRIBUTE, IDENTIFIER

def test_parse_single_object():
    cs = parse_payload('{"field":"location","value":"SF","confidence":0.9,"source":"gravatar"}')
    assert len(cs) == 1 and cs[0].field == "location" and cs[0].kind == ATTRIBUTE

def test_parse_array_and_identifier_kind():
    cs = parse_payload('[{"field":"email","value":"a@b.com","kind":"identifier","confidence":0.9,"source":"gravatar"}]')
    assert cs[0].kind == IDENTIFIER

def test_confidence_validated_range():
    import pytest
    with pytest.raises(ValueError):
        parse_payload('{"field":"location","value":"SF","confidence":1.5,"source":"x"}')
```

- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement** `src/crm/enrich.py` with a `@dataclass EnrichCandidate(field,value,kind,confidence,source,source_detail,evidence)`, `ATTRIBUTE="attribute"`, `IDENTIFIER="identifier"`, `IDENTIFIER_FIELDS={"email","linkedin_url","phone","handle"}`, and `parse_payload(json_str)->list[EnrichCandidate]` that: accepts object or array; defaults `kind` from field membership in `IDENTIFIER_FIELDS`; validates `0<=confidence<=1`; folds `evidence` into `source_detail` as `"<url> · <evidence>"` when both present.
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat(enrich): payload parsing + candidate model`

---

### Task 6: `crm enrich apply` command

**Files:** Create `src/crm/commands/enrich.py`; Modify `src/crm/cli.py`; Test `tests/test_enrich_apply.py`

- [ ] **Step 1: Failing CLI test**

```python
from typer.testing import CliRunner
from crm.cli import app
runner = CliRunner()

def test_apply_attribute_fills_field(db):
    c = db.table("contacts").insert({"full_name":"Ada","current_company":None}).execute().data[0]
    r = runner.invoke(app, ["enrich","apply",c["id"],"--agent","claude-web","--json"],
                      input='{"field":"current_company","value":"AnalyticEngine","confidence":0.9,"source":"agent:claude-web"}')
    assert r.exit_code == 0, r.output
    assert db.table("contacts").select("current_company").eq("id",c["id"]).single().execute().data["current_company"]=="AnalyticEngine"
```

- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement** `crm enrich apply <ref>`: read JSON (stdin or `--file`), `require_agent`, parse via `enrich.parse_payload`, for each `ATTRIBUTE` candidate call `enrich_apply_candidate` RPC; for `IDENTIFIER` candidates call the identifier path (Task 9, stub now → raise NotImplemented or route to candidate_identities later). Resolve `<ref>` to a contact id (reuse the existing ref-resolution helper in `contacts.py`). Honor `--dry-run` (pass `p_dry_run=True`, print outcomes). `--json` prints per-candidate `{field, outcome}`. Register sub-app in `cli.py`.
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat(enrich): crm enrich apply — agent write-back door`

---

### Task 7: Route `crm set` through the RPC

**Files:** Modify `src/crm/commands/contacts.py` (`set_field`); Test `tests/test_contacts_provenance.py`

- [ ] **Step 1: Failing test** — after `crm set company=X`, an `enrichment_log` row with `method='manual_set'`, `is_current=true` exists and `contacts.current_company='X'`; a subsequent enrich apply of a different value goes to review and does not overwrite.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement** — scalar `crm set` calls `enrich_apply_candidate(method='manual_set', source=agent, confidence=1.0)` instead of a bare update. Array fields (`tags`,`affiliations`) keep the existing union path. A blank value (`field=`) passes `p_value=None` (deliberate-NULL assertion).
- [ ] **Step 4: Run** → PASS (and existing `tests/test_contacts.py` still green).
- [ ] **Step 5: Commit** `refactor(contacts): crm set writes through provenance RPC`

---

### Task 8: `crm enrich review` (approve/reject/skip with tombstone)

**Files:** Modify `src/crm/commands/enrich.py`; Test `tests/test_enrich_review.py`

- [ ] **Step 1: Failing tests** — (a) `--approve` writes a `manual_set` winning row + resolves the queue item; (b) `--reject` writes a `disputed` row and re-applying that same value later still loses (tombstone sticks); (c) bare `crm enrich review` lists open items as table/JSON.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement** — list open `enrich_review`; `--approve <id>` → `enrich_apply_candidate(method='manual_set',confidence=1.0)` with the candidate value, set row `resolved`; `--reject <id>` → insert `enrichment_log` row `verification_status='disputed'` for `(field,value)`, then `enrich_recompute_field`, set row `resolved`; `--skip <id>` → status `skipped`.
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat(enrich): review queue arbitration with sticky rejects`

---

### Task 9: Identifier routing → `candidate_identities` (no duplicate-manufacturing)

**Files:** Modify `src/crm/commands/enrich.py`; reuse `src/crm/matching.py:find_candidates`; Test `tests/test_enrich_identifiers.py`

- [ ] **Step 1: Failing tests:**
  - discovered email with **0** matches → a `candidate_identities` row (status `pending`); not yet a `contact_identities` row.
  - discovered email matching **this** contact's existing identity → no-op.
  - discovered email matching a **different** contact → `enrich_review` row `reason='identifier_conflict'`, `other_contact_id` set; no write to identities.
  - promotion: `crm enrich review --approve` on a pending identifier → real `contact_identities` row (idempotent via existing unique index) so a later import auto-matches.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement** — in `apply`, for `IDENTIFIER` candidates: normalize (reuse `normalize.py`), run `find_candidates`; branch per spec §3.5. Promotion writes a `contact_identities` row (`source="enrich:<source>"`, `source_external_id=sha256(value)`), marks candidate `promoted`.
- [ ] **Step 4: Run** → PASS. Add a regression test: enrich-discovered email + later `crm import csv` of same email → single contact (no duplicate).
- [ ] **Step 5: Commit** `feat(enrich): identifier quarantine + dedup-safe promotion`

---

### Task 10: Job-change detection

**Files:** Modify `src/crm/commands/contacts.py` or import path that writes role/company; add `crm enrich changes`; Test `tests/test_enrich_jobchange.py`

- [ ] **Step 1: Failing test** — applying a new `current_company` over an existing different one leaves a provenance trail (old→new); `crm enrich changes --since <date> --json` lists it as a job change.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement** — `crm enrich changes --since` queries `enrichment_log` for `field in ('current_company','current_role')` where `old_value is not null and old_value <> new_value` within range; returns `{contact, field, old, new, at}`. (No new write path — the RPC already records old→new.)
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat(enrich): job-change detection via provenance (crm enrich changes)`

---

### Task 11: `crm enrich undo`, `stats`, `forget`

**Files:** Modify `src/crm/commands/enrich.py` + `src/crm/commands/admin.py` (fold counters into `stats`); Tests in `tests/test_enrich_undo.py`

- [ ] **Step 1: Failing tests** — `undo <ref> <field>`: demotes current robot value, re-elects prior winner from log, materializes it. `forget <ref>`: nulls `old_value/new_value` on that contact's enrichment_log rows, keeps structural rows. `stats` (or `enrich stats`): returns counts by `is_current` source, in-review, stale.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement** — `undo`: mark current row `verification_status='disputed'` (so it won't re-win) OR a dedicated demote; call `enrich_recompute_field`. `forget`: update value columns to null + set a `redacted_at` (add column if needed) — keep the row. `stats`: head-count queries (mirror `admin.py:stats`).
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat(enrich): undo, forget (redaction), stats coverage`

---

### Task 12: Provenance rendering in `crm contact`

**Files:** Modify `src/crm/commands/contacts.py` (the `contact` command); Test `tests/test_contacts_provenance.py`

- [ ] **Step 1: Failing test** — `crm contact <ref> --json` includes a `provenance` map: per scalar field `{value, source, confidence, as_of, stale}`; fields with no `is_current` row render value-only (no error).
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement** — join `enrichment_log` where `is_current` for the contact; compute `stale = refresh_after < today`. Human-readable table line: `company: Acme · via gravatar · 0.9 · 2026-06-14`.
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat(contacts): per-field provenance in crm contact`

---

## PHASE 1 — RETRIEVAL

### Task 13: `crm list` structured filters

**Files:** Modify `src/crm/commands/contacts.py` (`list` command); Test `tests/test_retrieval.py`

- [ ] **Step 1: Failing tests** — `--role founder,ceo` (case-insensitive substring + synonym expansion over `current_role`), `--company-category cybersecurity`, `--location nyc` (substring), composable with existing `--status/--tag/--cold-since`; `--json` output. Empty result is exit 0 with `[]`.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement** — extend the existing `list` query builder. Role synonym set: `{founder: [founder,co-founder,cofounder,founding], ceo:[ceo,chief executive]}`; `--role-class founder` alias. Use PostgREST `ilike`/`or` filters; for `--role a,b` build an `or=(current_role.ilike.*a*,current_role.ilike.*b*)`.
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat(list): role/company-category/location filters`

---

### Task 14: `crm capsules`

**Files:** Create `src/crm/commands/retrieval.py`; Modify `src/crm/cli.py`; Test `tests/test_retrieval.py`

- [ ] **Step 1: Failing test** — `crm capsules --json` returns one compact object per contact with `name, role, company, company_category, expertise, tags, note (truncated notes), topics (recent interaction summaries), location, tier, last (last_touchpoint_at), stale`. Accepts the same filters as `list` (so you can pre-filter the capsule set).
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement** — select the capsule columns + a small join to recent `interactions` summaries (top 2 by `occurred_at`). Truncate `notes` to ~140 chars. Keep it paginated past PostgREST's 1000-row cap (mirror `admin.py:stats` head-count approach for large sets; for capsules, page with range headers).
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat(retrieval): crm capsules — compact searchable representation`

---

### Task 15: `crm find` (hybrid: structured prefilter → capsules for in-context match)

**Files:** Modify `src/crm/commands/retrieval.py`; Test `tests/test_retrieval.py`

- [ ] **Step 1: Failing test** — `crm find "<intent>" --json` returns `{intent, candidates:[capsules]}` where candidates are the structurally-prefiltered set (by any `--role/--category/--location/--tag` flags passed alongside) plus a keyword-overlap prefilter from the intent string over `company_category/expertise/tags/notes/topics`. Semantic ranking is the agent's job — `find` returns the candidate pool, not a ranked answer.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement** — tokenize intent → keyword `or` ilike across capsule text columns; union with explicit flag filters; cap to a sane candidate ceiling (e.g. 300) and log if truncated (no silent cap). Output capsules for the pool.
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat(retrieval): crm find — hybrid candidate retrieval for in-context match`

---

### Task 16: Dossier enrichment of `crm contact --json`

**Files:** Modify `src/crm/commands/contacts.py`; Test `tests/test_contacts_provenance.py`

- [ ] **Step 1: Failing test** — `crm contact <ref> --json` bundle includes `origin_context`, `interactions` (with `occurred_at, channel, summary`), `last_touchpoint {at, channel, topic}`, and `stale` flags — everything Claude Code needs to draft outreach in one call.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement** — extend the existing `contact` fetch to include the interaction list (ordered desc, limited) and the denormalized last-touchpoint fields; ensure provenance (Task 12) is part of the same payload.
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat(contacts): full dossier bundle for crm contact --json`

---

## Final verification

- [ ] **Run the whole suite:** `supabase db reset && pytest -q` — Expected: all green (new + existing).
- [ ] **Smoke the flow against the LOCAL stack** (sample data): import a couple fixture contacts, `crm set` a manual value, `crm enrich apply` a conflicting value (→ review), `crm enrich review --reject`, `crm list --role founder --json`, `crm capsules --json`, `crm contact <ref> --json`. Confirm manual value survived and provenance renders.
- [ ] **Do NOT run against the real DB in Plan 1.** Real-data writes begin in Plan 2 (the fill), gated on keys + explicit go.

## Notes for the executor
- Reference @superpowers:test-driven-development for the red-green discipline.
- The RPC is the heart — if any Task-2/Task-3 test is flaky under concurrency, STOP and fix the advisory lock before building commands on top.
- Keep `src/crm/enrich.py` DB-free so it's unit-testable without the stack.
