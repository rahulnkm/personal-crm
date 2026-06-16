# tests/test_bulk_log.py
"""Behavioral tests for `crm bulk log` (Task 2.6).

Run against the local Supabase stack via the `db` fixture.
Always `supabase db reset` before running to apply migrations fresh.
"""
import json
from unittest.mock import patch

from typer.testing import CliRunner

from crm.cli import app
from tests._spy import CountingClient

runner = CliRunner()


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _contact(db, name, **kw):
    defaults = {
        "connection_status": "contact_on_file",
        "closeness_tier": "none",
    }
    return db.table("contacts").insert(
        {**defaults, "full_name": name, **kw}
    ).execute().data[0]


# ---------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------

def test_invalid_kind_exits_1(db):
    """An unrecognised --kind must exit 1 before any DB work."""
    c = _contact(db, "Kind Test", connection_status="in_network")
    r = runner.invoke(app, [
        "bulk", "log",
        "--kind", "chat",
        "--status", "in_network", "--yes", "--agent", "rahul",
    ])
    assert r.exit_code == 1, r.output
    assert "chat" in r.output


def test_invalid_date_exits_1(db):
    """A malformed --date (not YYYY-MM-DD) must exit 1."""
    c = _contact(db, "Date Test", connection_status="in_network")
    r = runner.invoke(app, [
        "bulk", "log",
        "--kind", "email",
        "--date", "nope",
        "--status", "in_network", "--yes", "--agent", "rahul",
    ])
    assert r.exit_code == 1, r.output
    assert "nope" in r.output


# ---------------------------------------------------------------------------
# happy path: chunked writes + bump
# ---------------------------------------------------------------------------

def test_happy_path_interactions_inserted_and_bumped(db):
    """3 contacts → 3 interactions inserted, each bumped to 2026-06-01; chunked
    at CHUNK=2 → 2 interactions.insert calls + 2 bump RPC calls."""
    contacts = [
        _contact(db, f"BulkLog {i}", connection_status="in_network")
        for i in range(3)
    ]
    ids = {c["id"] for c in contacts}

    import crm.bulk as bulk_mod
    import crm.commands.bulk as bulk_cmd
    spy = CountingClient(db)
    with patch("crm.commands.bulk.get_client", return_value=spy), \
            patch.object(bulk_cmd, "CHUNK", 2), \
            patch.object(bulk_mod, "CHUNK", 2):
        r = runner.invoke(app, [
            "bulk", "log",
            "--kind", "email",
            "--summary", "newsletter",
            "--date", "2026-06-01",
            "--status", "in_network",
            "--yes", "--agent", "rahul",
        ])

    assert r.exit_code == 0, r.output

    # 3 interaction rows inserted, correct fields
    interactions = db.table("interactions").select(
        "contact_id,kind,summary,logged_by,occurred_at"
    ).execute().data
    assert len(interactions) == 3
    assert {i["contact_id"] for i in interactions} == ids
    for row in interactions:
        assert row["kind"] == "email"
        assert row["summary"] == "newsletter"
        assert row["logged_by"] == "rahul"
        assert row["occurred_at"] == "2026-06-01"

    # last_touchpoint_at bumped on every contact
    for c in contacts:
        row = db.table("contacts").select(
            "last_touchpoint_at,last_touchpoint_topic"
        ).eq("id", c["id"]).single().execute().data
        assert row["last_touchpoint_at"] == "2026-06-01"
        assert row["last_touchpoint_topic"] == "newsletter"

    # CHUNK=2, 3 ids → 2 interactions.insert calls, 2 bump RPC calls
    assert spy.count("interactions", "insert") == 2
    assert spy.rpc_count("bulk_bump_last_touchpoint") == 2


# ---------------------------------------------------------------------------
# JSON shape
# ---------------------------------------------------------------------------

def test_json_shape(db):
    """--json emits expected shape: dry_run=false, cohort_count=3, changed_count=3."""
    for i in range(3):
        _contact(db, f"JSON Log {i}", connection_status="in_network")

    r = runner.invoke(app, [
        "bulk", "log",
        "--kind", "call",
        "--summary", "check-in",
        "--date", "2026-06-01",
        "--status", "in_network",
        "--yes", "--json", "--agent", "rahul",
    ])

    assert r.exit_code == 0, r.output
    data = json.loads(r.output)
    assert data["dry_run"] is False
    assert data["cohort_count"] == 3
    assert data["changed_count"] == 3
    assert isinstance(data["affected"], list)
    assert len(data["affected"]) == 3


# ---------------------------------------------------------------------------
# dry-run: no writes
# ---------------------------------------------------------------------------

def test_dry_run_no_writes(db):
    """--dry-run emits preview JSON (dry_run=true, no changed_count) without touching DB."""
    for i in range(3):
        _contact(db, f"Dry Log {i}", connection_status="in_network")

    import crm.bulk as bulk_mod
    import crm.commands.bulk as bulk_cmd
    spy = CountingClient(db)
    with patch("crm.commands.bulk.get_client", return_value=spy), \
            patch.object(bulk_cmd, "CHUNK", 2), \
            patch.object(bulk_mod, "CHUNK", 2):
        r = runner.invoke(app, [
            "bulk", "log",
            "--kind", "email",
            "--status", "in_network",
            "--dry-run", "--json",
        ])

    assert r.exit_code == 0, r.output
    data = json.loads(r.output)
    assert data["dry_run"] is True
    assert data["cohort_count"] == 3
    assert "changed_count" not in data

    # no interactions inserted, no RPC fired
    assert spy.count("interactions", "insert") == 0
    assert spy.rpc_count("bulk_bump_last_touchpoint") == 0
    assert db.table("interactions").select("id").execute().data == []


# ---------------------------------------------------------------------------
# write without --yes exits 2
# ---------------------------------------------------------------------------

def test_write_without_yes_exits_2(db):
    """Attempting a write without --yes must be refused (exit 2)."""
    _contact(db, "No Yes", connection_status="in_network")
    r = runner.invoke(app, [
        "bulk", "log",
        "--kind", "email",
        "--status", "in_network",
    ])
    assert r.exit_code == 2, r.output
