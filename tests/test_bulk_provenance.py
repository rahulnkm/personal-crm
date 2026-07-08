"""Task 5: `bulk set` rejoins survivorship (stale-election bug).

Before the fix, bulk set wrote the golden column directly and hand-rolled a
non-current enrichment_log row. Result: the still-is_current provenance row
kept claiming the pre-bulk value, and a later enrich_recompute_field would
resurrect that old value onto the column. The fix routes bulk set through
enrich_apply_candidate (manual_set) so the elected row always matches the column.
"""
import json

from typer.testing import CliRunner

from crm.cli import app

runner = CliRunner()

# >=20 chars so the narrative/expertise-style span gate never trips (belt+braces;
# current_company isn't gated, but the payload shape mirrors real agent output)
DETAIL = "grounding span: 'now at OldCo' from profile page, 2026-07-01"


def _seed(db, name, **kw):
    defaults = {"connection_status": "in_network", "closeness_tier": "none"}
    return db.table("contacts").insert(
        {**defaults, "full_name": name, **kw}).execute().data[0]


def _enrich_company(cid, value="OldCo"):
    """`enrich apply` a scalar via survivorship → creates the is_current=true row."""
    payload = json.dumps([{"field": "current_company", "value": value,
                           "source": "gravatar", "confidence": 0.9,
                           "source_detail": DETAIL}])
    r = runner.invoke(app, ["enrich", "apply", cid, "--json"], input=payload)
    assert r.exit_code == 0, r.output
    assert json.loads(r.stdout)[0]["outcome"] == "golden"


def _bulk_set_company(value="NewCo"):
    r = runner.invoke(app, ["bulk", "set", f"current_company={value}",
                            "--status", "in_network", "--yes", "--json"])
    assert r.exit_code == 0, r.output
    return json.loads(r.stdout)


# ── the stale-election bug ────────────────────────────────────────────────────

def test_bulk_set_elects_current_provenance_row(db):
    """After bulk set, the is_current=true row must carry the bulk value —
    not the pre-bulk value the old direct-update path left elected."""
    c = _seed(db, "Stale Election")
    _enrich_company(c["id"])
    _bulk_set_company("NewCo")

    cur = (db.table("enrichment_log")
           .select("new_value,method,source,is_current")
           .eq("contact_id", c["id"]).eq("field", "current_company")
           .eq("is_current", True).execute().data)
    assert len(cur) == 1
    assert cur[0]["new_value"] == "NewCo"
    assert cur[0]["method"] == "manual_set"
    assert cur[0]["source"] == "rahul"

    col = (db.table("contacts").select("current_company")
           .eq("id", c["id"]).single().execute().data)
    assert col["current_company"] == "NewCo"


def test_recompute_after_bulk_set_does_not_resurrect(db):
    """A later election re-run must keep the bulk value on the column.

    The prior value is a manual_set (crm set) row — election ranks manual_set
    first, so the old path's stale row would have resurrected 'OldCo' here.
    """
    c = _seed(db, "No Resurrect")
    r = runner.invoke(app, ["set", c["id"], "current_company=OldCo"])
    assert r.exit_code == 0, r.output
    _bulk_set_company("NewCo")

    db.rpc("enrich_recompute_field",
           {"p_contact_id": c["id"], "p_field": "current_company"}).execute()

    col = (db.table("contacts").select("current_company")
           .eq("id", c["id"]).single().execute().data)
    assert col["current_company"] == "NewCo"


# ── blank-clear parity with crm set ───────────────────────────────────────────

def test_bulk_set_blank_clears_as_manual_null(db):
    """`field=` (blank) is a deliberate NULL clear routed through manual_set,
    matching single `crm set` semantics."""
    c = _seed(db, "Blank Clear", current_company="OldCo")
    _bulk_set_company("")

    col = (db.table("contacts").select("current_company")
           .eq("id", c["id"]).single().execute().data)
    assert col["current_company"] is None

    cur = (db.table("enrichment_log")
           .select("new_value,method")
           .eq("contact_id", c["id"]).eq("field", "current_company")
           .eq("is_current", True).execute().data)
    assert len(cur) == 1
    assert cur[0]["new_value"] is None
    assert cur[0]["method"] == "manual_set"


# ── output contract preserved ─────────────────────────────────────────────────

def test_bulk_set_json_contract_unchanged(db):
    ids = {_seed(db, f"Contract {i}")["id"] for i in range(3)}
    data = _bulk_set_company("NewCo")
    assert data["dry_run"] is False
    assert data["cohort_count"] == 3
    assert data["changed_count"] == 3
    assert set(data["affected"]) == ids
