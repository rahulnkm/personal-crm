"""Tests for `crm bulk set` (Task 2.4).

Run against the local Supabase stack via the `db` fixture.
Always `supabase db reset` before running to apply migrations fresh.

The verb validates the assignment (mirroring single `crm set`), then defers
the cohort + write-gate to `crm.bulk._gate`, then writes per CHUNK-sized slice
and emits the final tally itself (the gate returns ids on the happy path; it
does NOT emit there).
"""
import json
from unittest.mock import patch

from typer.testing import CliRunner

from crm.cli import app
from tests._spy import CountingClient

runner = CliRunner()


def _seed(db, name, **kw):
    defaults = {"connection_status": "contact_on_file", "closeness_tier": "none"}
    return db.table("contacts").insert({**defaults, "full_name": name, **kw}).execute().data[0]


# ── validation: mirrors single set_field, with the array-field divergence ─────

def test_no_equals_exits_2(db):
    r = runner.invoke(app, ["bulk", "set", "closeness_tier",
                            "--status", "in_network", "--yes"])
    assert r.exit_code == 2, r.output


def test_non_settable_field_exits_1(db):
    r = runner.invoke(app, ["bulk", "set", "bogus_field=x",
                            "--status", "in_network", "--yes"])
    assert r.exit_code == 1, r.output


def test_array_field_exits_2(db):
    """tags is an ARRAY_FIELD — bulk set refuses it (Exit 2), pointing at bulk tag."""
    r = runner.invoke(app, ["bulk", "set", "tags=founder",
                            "--status", "in_network", "--yes"])
    assert r.exit_code == 2, r.output


def test_bad_enum_value_exits_1(db):
    r = runner.invoke(app, ["bulk", "set", "connection_status=bogus",
                            "--status", "in_network", "--yes"])
    assert r.exit_code == 1, r.output


# ── happy path: writes + audit rows, chunked ──────────────────────────────────

def test_happy_path_updates_and_logs_chunked(db):
    """3 in_network contacts, CHUNK=2 → 2 read + 2 update + 2 insert calls,
    all 3 get the new tier, 3 enrichment_log rows with method='bulk_set' and the
    captured old_value."""
    rows = [_seed(db, f"Bulk Set {i}", connection_status="in_network",
                  closeness_tier="none") for i in range(3)]
    ids = {r["id"] for r in rows}

    spy = CountingClient(db)
    # bulk_set's read+update use .in_() (URL), so the loop chunks by URL_CHUNK.
    # commands.bulk does `from crm.bulk import URL_CHUNK`, so patch the copy bound
    # in commands.bulk (the one the loop reads).
    import crm.commands.bulk as bulk_cmd
    with patch("crm.commands.bulk.get_client", return_value=spy), \
            patch.object(bulk_cmd, "URL_CHUNK", 2):
        r = runner.invoke(app, ["bulk", "set", "closeness_tier=t2_dm",
                                "--status", "in_network", "--yes", "--agent", "rahul"])
    assert r.exit_code == 0, r.output

    # 3 ids at CHUNK=2 → two slices → two writes + two per-slice reads.
    # _gate also resolves the cohort with one contacts.select("id"), so the
    # contacts select count is the cohort read (1) + the two per-slice reads (2).
    assert spy.count("contacts", "select") == 3
    assert spy.count("contacts", "update") == 2
    assert spy.count("enrichment_log", "insert") == 2

    # all three updated
    updated = db.table("contacts").select("id,closeness_tier").in_("id", list(ids)).execute().data
    assert all(u["closeness_tier"] == "t2_dm" for u in updated)

    # 3 audit rows, captured old_value
    logs = db.table("enrichment_log").select(
        "contact_id,field,old_value,new_value,source,method"
    ).eq("method", "bulk_set").execute().data
    assert len(logs) == 3
    assert {lg["contact_id"] for lg in logs} == ids
    for lg in logs:
        assert lg["field"] == "closeness_tier"
        assert lg["old_value"] == "none"
        assert lg["new_value"] == "t2_dm"
        assert lg["source"] == "rahul"


# ── JSON shape ────────────────────────────────────────────────────────────────

def test_json_shape(db):
    for i in range(3):
        _seed(db, f"JSON Set {i}", connection_status="in_network")
    r = runner.invoke(app, ["bulk", "set", "closeness_tier=t2_dm",
                            "--status", "in_network", "--yes", "--json", "--agent", "rahul"])
    assert r.exit_code == 0, r.output
    data = json.loads(r.output)
    assert data["dry_run"] is False
    assert data["cohort_count"] == 3
    assert data["changed_count"] == 3
    assert isinstance(data["affected"], list) and len(data["affected"]) == 3


# ── dry-run: no writes ────────────────────────────────────────────────────────

def test_dry_run_no_writes(db):
    c = _seed(db, "Dry Set", connection_status="in_network", closeness_tier="none")
    r = runner.invoke(app, ["bulk", "set", "closeness_tier=t2_dm",
                            "--status", "in_network", "--dry-run", "--json"])
    assert r.exit_code == 0, r.output
    data = json.loads(r.output)
    assert data["dry_run"] is True
    assert "changed_count" not in data
    # nothing written
    after = db.table("contacts").select("closeness_tier").eq("id", c["id"]).single().execute().data
    assert after["closeness_tier"] == "none"
    assert db.table("enrichment_log").select("id").eq("method", "bulk_set").execute().data == []


# ── write without --yes is refused ────────────────────────────────────────────

def test_write_without_yes_exits_2(db):
    _seed(db, "No Yes", connection_status="in_network")
    r = runner.invoke(app, ["bulk", "set", "closeness_tier=t2_dm",
                            "--status", "in_network"])
    assert r.exit_code == 2, r.output


# ── smoke ─────────────────────────────────────────────────────────────────────

def test_bare_bulk_runs():
    r = runner.invoke(app, ["bulk"])
    # a Typer group with no subcommand prints help; exit 0 or 2 are both fine,
    # what matters is it doesn't crash with a traceback
    assert r.exit_code in (0, 2), r.output


def test_bulk_set_help():
    r = runner.invoke(app, ["bulk", "set", "--help"])
    assert r.exit_code == 0, r.output
    assert "set" in r.output.lower()
