"""Tests for _apply_filters / _resolve_cohort (Task 2.1).

Step 1: these tests are written BEFORE the helpers exist, so the cohort tests
fail until Step 3 lands.  The list_contacts characterization tests must PASS
against the current code unchanged.
"""
import json
from datetime import date, timedelta

import pytest
from typer.testing import CliRunner

from crm.cli import app

runner = CliRunner()


# ── helpers ─────────────────────────────────────────────────────────────────

def _seed(db, name, **kw):
    defaults = {
        "connection_status": "contact_on_file",
        "closeness_tier": "none",
    }
    return db.table("contacts").insert({**defaults, "full_name": name, **kw}).execute().data[0]


# ── _resolve_cohort: per-filter tests ───────────────────────────────────────

def test_resolve_cohort_filter_status(db):
    from crm.bulk import _resolve_cohort

    _seed(db, "In Person", connection_status="in_network")
    _seed(db, "On File", connection_status="contact_on_file")

    ids = _resolve_cohort(db, status="in_network")
    rows = db.table("contacts").select("id,full_name").in_("id", ids).execute().data
    names = {r["full_name"] for r in rows}
    assert "In Person" in names
    assert "On File" not in names


def test_resolve_cohort_filter_tier(db):
    from crm.bulk import _resolve_cohort

    _seed(db, "Tier1 Alice", closeness_tier="t1_irl_messaging")
    _seed(db, "Tier4 Bob", closeness_tier="t4_public")

    ids = _resolve_cohort(db, tier="t1_irl_messaging")
    rows = db.table("contacts").select("full_name").in_("id", ids).execute().data
    names = {r["full_name"] for r in rows}
    assert "Tier1 Alice" in names
    assert "Tier4 Bob" not in names


def test_resolve_cohort_filter_tag(db):
    from crm.bulk import _resolve_cohort

    # register tag via CLI (same pattern used in test_contacts.py)
    runner.invoke(app, ["tags", "add", "founder", "--desc", "founder tag"])

    _seed(db, "Tagged Carol", tags=["founder"])
    _seed(db, "Untagged Dave")

    ids = _resolve_cohort(db, tag="founder")
    rows = db.table("contacts").select("full_name").in_("id", ids).execute().data
    names = {r["full_name"] for r in rows}
    assert "Tagged Carol" in names
    assert "Untagged Dave" not in names


def test_resolve_cohort_filter_affiliation(db):
    from crm.bulk import _resolve_cohort

    _seed(db, "YC Eve", affiliations=["yc"])
    _seed(db, "None Frank")

    ids = _resolve_cohort(db, affiliation="yc")
    rows = db.table("contacts").select("full_name").in_("id", ids).execute().data
    names = {r["full_name"] for r in rows}
    assert "YC Eve" in names
    assert "None Frank" not in names


def test_resolve_cohort_filter_cold_since(db):
    """cold_since=1 → contacts with last_touchpoint_at older than ~30 days ago OR null."""
    from crm.bulk import _resolve_cohort

    cutoff = date.today() - timedelta(days=30)
    old_date = (cutoff - timedelta(days=5)).isoformat()
    recent_date = (date.today() - timedelta(days=1)).isoformat()

    _seed(db, "Cold Gina", last_touchpoint_at=old_date)
    _seed(db, "Warm Hank", last_touchpoint_at=recent_date)
    _seed(db, "Never Iris")  # null last_touchpoint_at

    ids = _resolve_cohort(db, cold_since=1)
    rows = db.table("contacts").select("full_name").in_("id", ids).execute().data
    names = {r["full_name"] for r in rows}
    assert "Cold Gina" in names
    assert "Never Iris" in names  # null is included
    assert "Warm Hank" not in names


def test_resolve_cohort_and_composition(db):
    """Two filters compose with AND — only the intersection is returned."""
    from crm.bulk import _resolve_cohort

    _seed(db, "Match Jack",
          connection_status="in_network", closeness_tier="t1_irl_messaging")
    _seed(db, "Wrong Status Karen",
          connection_status="contact_on_file", closeness_tier="t1_irl_messaging")
    _seed(db, "Wrong Tier Leo",
          connection_status="in_network", closeness_tier="t4_public")

    ids = _resolve_cohort(db, status="in_network", tier="t1_irl_messaging")
    rows = db.table("contacts").select("full_name").in_("id", ids).execute().data
    names = {r["full_name"] for r in rows}
    assert "Match Jack" in names
    assert "Wrong Status Karen" not in names
    assert "Wrong Tier Leo" not in names


def test_resolve_cohort_returns_distinct_ids(db):
    """Returned ids must be unique (len == len(set))."""
    from crm.bulk import _resolve_cohort

    _seed(db, "Distinct Mike", connection_status="in_network")
    _seed(db, "Distinct Nina", connection_status="in_network")

    ids = _resolve_cohort(db, status="in_network")
    assert len(ids) == len(set(ids)), "duplicate ids in result"


# ── _resolve_cohort: pagination past PAGE ────────────────────────────────────

def test_resolve_cohort_paginates_past_page(db, monkeypatch):
    """With PAGE=2, seeding 3 matching contacts must return all 3 — exercises
    the range-loop short-page exit that fires when a page is shorter than PAGE."""
    import crm.bulk as bulk_mod

    monkeypatch.setattr(bulk_mod, "PAGE", 2)

    from crm.bulk import _resolve_cohort

    for i in range(3):
        _seed(db, f"Page Contact {i}", connection_status="in_network")

    ids = _resolve_cohort(db, status="in_network")
    rows = db.table("contacts").select("full_name").in_("id", ids).execute().data
    names = [r["full_name"] for r in rows]
    # all three must be present
    assert sum(1 for n in names if n.startswith("Page Contact")) == 3


# ── list_contacts: characterization tests (must pass BEFORE refactor) ────────

def test_list_contacts_status_filter_characterization(db):
    """crm list --status in_network returns only in_network rows, all 9 columns."""
    r = runner.invoke(app, ["add", "Char Alice", "--status", "in_network",
                            "--tier", "t1_irl_messaging", "--agent", "rahul"])
    assert r.exit_code == 0, r.output
    runner.invoke(app, ["add", "Char Bob", "--status", "contact_on_file", "--agent", "rahul"])

    r = runner.invoke(app, ["list", "--status", "in_network", "--json"])
    assert r.exit_code == 0, r.output
    rows = json.loads(r.output)

    names = [x["full_name"] for x in rows]
    assert "Char Alice" in names
    assert "Char Bob" not in names

    # verify the 9 expected columns are present in each row
    expected_cols = {
        "id", "full_name", "current_role", "current_company",
        "connection_status", "closeness_tier", "affiliations",
        "tags", "last_touchpoint_at",
    }
    for row in rows:
        assert expected_cols == set(row.keys()), (
            f"column set mismatch: got {set(row.keys())}"
        )


def test_list_contacts_tier_filter_characterization(db):
    """crm list --tier t2_dm returns only t2_dm rows."""
    runner.invoke(app, ["add", "Tier2 Carol", "--status", "in_network",
                        "--tier", "t2_dm", "--agent", "rahul"])
    runner.invoke(app, ["add", "Tier4 Dave", "--status", "in_network",
                        "--tier", "t4_public", "--agent", "rahul"])

    r = runner.invoke(app, ["list", "--tier", "t2_dm", "--json"])
    assert r.exit_code == 0, r.output
    rows = json.loads(r.output)
    names = [x["full_name"] for x in rows]
    assert "Tier2 Carol" in names
    assert "Tier4 Dave" not in names


def test_list_contacts_cold_since_characterization(db):
    """crm list --cold-since 1 returns contacts with old or null last_touchpoint_at."""
    cutoff = date.today() - timedelta(days=30)
    old_date = (cutoff - timedelta(days=5)).isoformat()
    recent_date = (date.today() - timedelta(days=1)).isoformat()

    cold_id = _seed(db, "Cold Eve", last_touchpoint_at=old_date)["id"]
    _seed(db, "Warm Frank", last_touchpoint_at=recent_date)
    null_id = _seed(db, "Never Grace")["id"]

    r = runner.invoke(app, ["list", "--cold-since", "1", "--json"])
    assert r.exit_code == 0, r.output
    rows = json.loads(r.output)
    ids = {x["id"] for x in rows}
    assert cold_id in ids
    assert null_id in ids
    # Warm Frank must not appear
    warm_ids = db.table("contacts").select("id").eq("full_name", "Warm Frank").execute().data
    assert not (warm_ids and warm_ids[0]["id"] in ids)


def test_list_contacts_order_nullsfirst_characterization(db):
    """list_contacts orders by last_touchpoint_at ASC nulls first."""
    cutoff = date.today() - timedelta(days=30)
    old_date = (cutoff - timedelta(days=10)).isoformat()
    mid_date = (cutoff - timedelta(days=5)).isoformat()

    _seed(db, "Null First", last_touchpoint_at=None)
    _seed(db, "Old Second", last_touchpoint_at=old_date)
    _seed(db, "Mid Third", last_touchpoint_at=mid_date)

    r = runner.invoke(app, ["list", "--json"])
    assert r.exit_code == 0, r.output
    rows = json.loads(r.output)
    names = [x["full_name"] for x in rows]

    null_idx = names.index("Null First")
    old_idx = names.index("Old Second")
    mid_idx = names.index("Mid Third")
    assert null_idx < old_idx < mid_idx, (
        f"order wrong: null@{null_idx}, old@{old_idx}, mid@{mid_idx}"
    )
