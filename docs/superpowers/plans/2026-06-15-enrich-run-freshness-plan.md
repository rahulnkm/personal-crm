# enrich run fixes + freshness clock — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development or superpowers:executing-plans. Steps use `- [ ]`.

**Goal:** Fix `crm enrich run` scoping/counting (#2) and add a per-field freshness clock (#3).

**Architecture:** One additive migration (`enrich_refresh_after` SQL helper + `create or replace enrich_apply_candidate` to stamp `refresh_after`); Python edits to `crm enrich run` (`--limit` slice, `--all`, `reviewed` tally, `--due`) plus a new `crm enrich due` command.

**Tech Stack:** Python/Typer, Postgres plpgsql, pytest vs local Supabase (55322).

**Spec:** `docs/superpowers/specs/2026-06-15-enrich-run-freshness-design.md`. Conventions: apply migration via `psql "postgresql://postgres:postgres@127.0.0.1:55322/postgres" -v ON_ERROR_STOP=1 -f <file>` (db reset blocked). Commit only touched files; never commit `supabase/config.toml` / `.env.local`.

---

### Task 1: Migration 0013 — freshness helper + stamp refresh_after

**Files:** Create `supabase/migrations/0013_enrich_freshness.sql`; Test `tests/test_enrich_freshness.py`

- [ ] **Step 1 — failing tests** (`db.rpc`, mirror `tests/test_enrich_rpc.py` helpers)
```python
def test_refresh_after_ttls(db):
    f = lambda field, method: db.rpc("enrich_refresh_after", {"p_field": field, "p_method": method}).execute().data
    import datetime
    d90 = (datetime.date.today()+datetime.timedelta(days=90)).isoformat()
    assert f("current_company","enrich_api") == d90
    assert f("location","enrich_api") == (datetime.date.today()+datetime.timedelta(days=180)).isoformat()
    assert f("origin_context","enrich_api") is None      # stable → never
    assert f("current_company","manual_set") is None     # manual → never

def test_apply_stamps_refresh_after(db):
    c = db.table("contacts").insert({"full_name":"Fresh","current_company":None}).execute().data[0]
    db.rpc("enrich_apply_candidate", {"p_contact_id":c["id"],"p_field":"current_company","p_value":"Acme",
        "p_method":"enrich_api","p_source":"x","p_confidence":0.9,"p_source_detail":None,"p_dry_run":False}).execute()
    row = db.table("enrichment_log").select("refresh_after").eq("contact_id",c["id"]).eq("field","current_company").eq("is_current",True).single().execute().data
    assert row["refresh_after"] is not None
    # stable field → null
    db.rpc("enrich_apply_candidate", {"p_contact_id":c["id"],"p_field":"origin_context","p_value":"met at X",
        "p_method":"enrich_api","p_source":"x","p_confidence":0.9,"p_source_detail":None,"p_dry_run":False}).execute()
    row2 = db.table("enrichment_log").select("refresh_after").eq("contact_id",c["id"]).eq("field","origin_context").eq("is_current",True).single().execute().data
    assert row2["refresh_after"] is None
```
- [ ] **Step 2 — run, expect FAIL** (`enrich_refresh_after` missing). `uv run pytest tests/test_enrich_freshness.py -v`
- [ ] **Step 3 — write migration.** Add the helper, then `create or replace enrich_apply_candidate` by copying its CURRENT definition (get it exactly via `psql ... -c "\sf enrich_apply_candidate"` or from `0006_enrichment_substrate.sql`) and adding `refresh_after` to the provenance INSERT.
```sql
create or replace function enrich_refresh_after(p_field text, p_method text)
returns date language sql immutable as $$
  select case
    when p_method = 'manual_set' then null
    when p_field in ('current_role','current_company','company_category') then current_date + 90
    when p_field in ('location','email_status') then current_date + 180
    else null
  end;
$$;
```
In the copied `enrich_apply_candidate`, change the INSERT from
`insert into enrichment_log (contact_id, field, old_value, new_value, source, confidence, method, source_detail) select …`
to add `, refresh_after` to the column list and `, enrich_refresh_after(p_field, p_method)` to the select list. Leave everything else (advisory lock, field-existence guard, disputed short-circuit, idempotency, recompute) byte-identical.
- [ ] **Step 4 — apply + pass.** `psql … -f supabase/migrations/0013_enrich_freshness.sql` then run the tests (PASS). Then full suite `uv run pytest -q` (no regressions).
- [ ] **Step 5 — commit** `feat(enrich): freshness clock — enrich_refresh_after + stamp refresh_after`

---

### Task 2: `crm enrich run` — limit slice, --all, reviewed tally

**Files:** Modify `src/crm/commands/enrich.py` (`run`, `_candidate_contacts`); Test `tests/test_enrich_run.py`

- [ ] **Step 1 — failing tests**
```python
def test_run_limit_caps_touched(db):  # --limit N considers exactly the first N candidates
    db.table("agents").upsert({"id":"x","description":"t"}, on_conflict="id").execute()
    for i in range(5):
        db.table("contacts").insert({"full_name":f"P{i}","connection_status":"in_network"}).execute()
    r = runner.invoke(app, ["enrich","run","--sources","gravatar","--limit","2","--dry-run","--json","--no-only-missing"])
    import json as J; s = J.loads(r.output)["summary"]
    assert s["contacts"] + s["no_email"] + s["skipped"] <= 2   # at most 2 contacts considered

def test_run_all_reaches_contact_on_file(db):
    db.table("agents").upsert({"id":"x","description":"t"}, on_conflict="id").execute()
    c = db.table("contacts").insert({"full_name":"OnFile","connection_status":"contact_on_file"}).execute().data[0]
    r = runner.invoke(app, ["enrich","run","--all","--limit","50","--dry-run","--json","--no-only-missing"])
    assert c["id"] in r.output   # contact_on_file now in scope
```
(`reviewed` tally is covered indirectly; add a unit asserting a `reviewed` key exists in the summary JSON.)
- [ ] **Step 2 — run, expect FAIL.**
- [ ] **Step 3 — implement.**
  - Add option `all_contacts: bool = typer.Option(False, "--all", help="Ignore the connection_status filter (whole DB)")`.
  - `contacts = _candidate_contacts(client, None if all_contacts else status, tier)`.
  - After the sort, replace the in-loop limit gate with a slice: `if limit is not None: contacts = contacts[:limit]`; delete the `if limit is not None and processed >= limit: break` and the now-unused `processed` gate (keep `processed` only if still referenced; otherwise drop).
  - Track reviews: add `"reviewed": 0` to `summary`; in the source loop set `reviewed_any = True` when `outcome == "review"`. In the status classification, add a branch: after `elif fields_written:` and before `else:` → `elif reviewed_any: status_label="reviewed"; summary["reviewed"] += 1`. Add `reviewed` to the non-JSON summary print line.
- [ ] **Step 4 — run tests (PASS) + full suite.**
- [ ] **Step 5 — commit** `fix(enrich): run --limit slices touched, add --all, tally reviewed`

---

### Task 3: `crm enrich due` + `run --due`

**Files:** Modify `src/crm/commands/enrich.py`; Test `tests/test_enrich_freshness.py`

- [ ] **Step 1 — failing tests**
```python
def test_due_lists_stale_excludes_fresh(db):
    stale = db.table("contacts").insert({"full_name":"Stale","connection_status":"in_network","closeness_tier":"t1_irl_messaging"}).execute().data[0]
    fresh = db.table("contacts").insert({"full_name":"Fresh2","connection_status":"in_network"}).execute().data[0]
    import datetime; past=(datetime.date.today()-datetime.timedelta(days=1)).isoformat(); fut=(datetime.date.today()+datetime.timedelta(days=30)).isoformat()
    db.table("enrichment_log").insert({"contact_id":stale["id"],"field":"current_company","new_value":"A","source":"x","method":"enrich_api","is_current":True,"refresh_after":past}).execute()
    db.table("enrichment_log").insert({"contact_id":fresh["id"],"field":"current_company","new_value":"B","source":"x","method":"enrich_api","is_current":True,"refresh_after":fut}).execute()
    r = runner.invoke(app, ["enrich","due","--json"])
    assert stale["id"] in r.output and fresh["id"] not in r.output
```
- [ ] **Step 2 — run, expect FAIL** (`due` command missing).
- [ ] **Step 3 — implement.**
  - Helper `_due_contact_ids(client) -> set[str]`: page `enrichment_log` `.select("contact_id").eq("is_current",True).lt("refresh_after", date.today().isoformat())`, collect distinct ids. (NULL refresh_after is excluded by `.lt` — desired.)
  - `@enrich_app.command("due")`: fetch those contacts (id, full_name, closeness_tier, current_company), sort by `_TIER_RANK`, print table or `--json`.
  - `run`: add `due: bool = typer.Option(False, "--due", help="Only contacts with a stale field")`; when set, after building `contacts`, filter `contacts = [c for c in contacts if c["id"] in _due_contact_ids(client)]`.
- [ ] **Step 4 — run tests (PASS) + full suite green.**
- [ ] **Step 5 — commit** `feat(enrich): crm enrich due + run --due (freshness-driven)`

---

## Final
- [ ] `uv run pytest -q` all green.
- [ ] Do NOT deploy 0013 to cloud here — that's the ship step (with explicit approval), alongside the 0006→0010 renumber + config.toml restore in finishing-a-development-branch.
