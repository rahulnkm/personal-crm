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

    # list now also surfaces company_category + location (enrichment retrieval needs
    # them; extra keys are backward-compatible for consumers).
    expected_cols = {
        "id", "full_name", "current_role", "current_company",
        "connection_status", "closeness_tier", "affiliations",
        "tags", "last_touchpoint_at", "company_category", "location",
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


# ── _gate / _emit: Task 2.2 ──────────────────────────────────────────────────

def _gate_call(db, *, status=None, tier=None, tag=None, affiliation=None,
               cold_since=None, all_=False, dry_run=False, yes=False,
               as_json=False, agent="rahul"):
    """Thin helper so individual tests only pass what they care about."""
    from crm.bulk import _gate
    return _gate(
        db,
        status=status, tier=tier, tag=tag, affiliation=affiliation,
        cold_since=cold_since, all_=all_, dry_run=dry_run, yes=yes,
        as_json=as_json, agent=agent,
    )


# ── guard-rail: no filter + not all_ ────────────────────────────────────────

def test_gate_no_filter_no_all_exits(db):
    """No filters + no --all must exit(2) with an error."""
    import typer
    with pytest.raises(typer.Exit) as exc_info:
        _gate_call(db)
    assert exc_info.value.exit_code == 2


# ── guard-rail: --all + filter ────────────────────────────────────────────────

def test_gate_all_with_filter_exits(db):
    """--all combined with a filter must exit(2)."""
    import typer
    with pytest.raises(typer.Exit) as exc_info:
        _gate_call(db, all_=True, status="in_network")
    assert exc_info.value.exit_code == 2


# ── guard-rail: not dry-run + not yes ────────────────────────────────────────

def test_gate_write_without_yes_exits(db):
    """A write path (not dry-run) without --yes must exit(2)."""
    import typer
    _seed(db, "Alice Write Guard", connection_status="in_network")
    with pytest.raises(typer.Exit) as exc_info:
        _gate_call(db, status="in_network", dry_run=False, yes=False)
    assert exc_info.value.exit_code == 2


# ── guard-rail: unregistered agent on write path ─────────────────────────────

def test_gate_unregistered_agent_exits(db):
    """An unregistered agent string on a write path must exit(1)."""
    import typer
    _seed(db, "Bob Agent Guard", connection_status="in_network")
    with pytest.raises(typer.Exit) as exc_info:
        _gate_call(db, status="in_network", yes=True, agent="ghost-agent-xyz")
    assert exc_info.value.exit_code == 1


# ── dry-run: bogus agent is NOT validated ────────────────────────────────────

def test_gate_dry_run_skips_agent_validation(db, capsys):
    """dry-run with a bogus agent must NOT raise, and must emit dry-run shape."""
    _seed(db, "Carol Dry Run", connection_status="in_network")
    result = _gate_call(db, status="in_network", dry_run=True, agent="totally-bogus-agent",
                        as_json=True)
    assert result is None  # STOP sentinel
    captured = capsys.readouterr()
    data = json.loads(captured.out.strip())
    assert data["dry_run"] is True
    assert data["cohort_count"] >= 1
    assert isinstance(data["affected"], list)


# ── dry-run + as_json: correct JSON shape ────────────────────────────────────

def test_gate_dry_run_json_shape(db, capsys):
    """dry-run + --json must emit {"dry_run": true, "cohort_count": N, "affected": [...]}."""
    _seed(db, "Dave JSON Dry", connection_status="in_network")
    result = _gate_call(db, status="in_network", dry_run=True, as_json=True)
    assert result is None
    data = json.loads(capsys.readouterr().out.strip())
    assert data["dry_run"] is True
    assert "cohort_count" in data
    assert "affected" in data
    assert "changed_count" not in data  # dry-run must NOT include changed_count


# ── empty cohort on write path ───────────────────────────────────────────────

def test_gate_empty_cohort_emits_zero_and_stops(db, capsys):
    """A filter matching nothing must emit count 0 and return None (not the ids)."""
    # seed nothing for this tag so the cohort is empty
    result = _gate_call(db, status="in_network", yes=True, as_json=True)
    assert result is None
    data = json.loads(capsys.readouterr().out.strip())
    assert data["dry_run"] is False
    assert data["cohort_count"] == 0
    assert data["affected"] == []
    assert data["changed_count"] == 0


# ── happy path: returns ids ───────────────────────────────────────────────────

def test_gate_happy_path_returns_ids(db):
    """A matching cohort on a write path (yes=True, valid agent) must return the ids."""
    row = _seed(db, "Eve Happy", connection_status="in_network")
    ids = _gate_call(db, status="in_network", yes=True, agent="rahul")
    assert ids is not None
    assert row["id"] in ids


# ── spy: agent validated exactly once ────────────────────────────────────────

def test_gate_agent_validated_exactly_once(db):
    """require_agent must hit the agents table exactly once on a write path."""
    from tests._spy import CountingClient
    _seed(db, "Frank Spy", connection_status="in_network")
    spy = CountingClient(db)
    _gate_call(spy, status="in_network", yes=True, agent="rahul")
    assert spy.count("agents", "select") == 1


# ── human output: dry-run human-readable ─────────────────────────────────────

def test_gate_dry_run_human_output(db, capsys):
    """Human dry-run must print a 'would affect N contacts:' style line."""
    _seed(db, "Grace Human Dry", connection_status="in_network")
    result = _gate_call(db, status="in_network", dry_run=True, as_json=False)
    assert result is None
    out = capsys.readouterr().out
    assert "would affect" in out
