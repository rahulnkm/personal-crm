# tests/test_bulk_tag.py
"""Behavioral tests for the bulk_add_tag RPC (migration 0009) and
the `crm bulk tag` CLI verb (Task 2.5).

Run against the local Supabase stack via the `db` fixture.
Always `supabase db reset` before running to apply migrations fresh.
"""
import json
import uuid
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
    return db.table("contacts").insert({**defaults, "full_name": name, **kw}).execute().data[0]


def _tags(db, contact_id) -> list[str]:
    """Fetch the current tags array for a given contact id."""
    row = db.table("contacts").select("tags").eq("id", contact_id).single().execute().data
    return row["tags"]


def _rpc(db, tag: str, ids: list[str]) -> list[str]:
    return db.rpc("bulk_add_tag", {"p_tag": tag, "p_ids": ids}).execute().data


# ---------------------------------------------------------------------------
# idempotent: returns only newly-affected ids
# ---------------------------------------------------------------------------

def test_bulk_add_tag_idempotent_returns_newly_affected(db):
    """Calling bulk_add_tag with p_ids including one already-tagged contact
    returns ONLY the 2 newly-affected ids; the pre-tagged one is excluded."""
    pre = _contact(db, "Pre-Tagged", tags=["vip"])
    c1  = _contact(db, "Contact A")
    c2  = _contact(db, "Contact B")

    affected = _rpc(db, "vip", [pre["id"], c1["id"], c2["id"]])

    # only the two fresh contacts should be returned
    assert set(affected) == {c1["id"], c2["id"]}
    # the pre-tagged one must NOT appear in the result
    assert pre["id"] not in affected


def test_bulk_add_tag_newly_tagged_contacts_have_tag(db):
    """The two contacts that lacked 'vip' must have it after the call."""
    c1 = _contact(db, "Fresh One")
    c2 = _contact(db, "Fresh Two")

    _rpc(db, "vip", [c1["id"], c2["id"]])

    assert "vip" in _tags(db, c1["id"])
    assert "vip" in _tags(db, c2["id"])


def test_bulk_add_tag_pre_tagged_unchanged_no_dup(db):
    """The already-tagged contact must still carry exactly one 'vip' — no duplicate."""
    pre = _contact(db, "Already VIP", tags=["vip"])
    c1  = _contact(db, "New Guy")

    _rpc(db, "vip", [pre["id"], c1["id"]])

    tags = _tags(db, pre["id"])
    assert tags.count("vip") == 1, f"duplicate 'vip' found: {tags}"


# ---------------------------------------------------------------------------
# sort order
# ---------------------------------------------------------------------------

def test_bulk_add_tag_result_is_sorted(db):
    """After adding 'mid' to a contact with tags ['zeta','alpha'],
    the resulting tags array must be ['alpha','mid','zeta'] (sorted asc)."""
    c = _contact(db, "Sort Test", tags=["zeta", "alpha"])

    _rpc(db, "mid", [c["id"]])

    assert _tags(db, c["id"]) == ["alpha", "mid", "zeta"]


# ---------------------------------------------------------------------------
# empty p_ids
# ---------------------------------------------------------------------------

def test_bulk_add_tag_empty_ids_returns_empty(db):
    """Passing an empty p_ids list returns [] and mutates nothing."""
    c = _contact(db, "Untouched")
    before = _tags(db, c["id"])

    result = _rpc(db, "vip", [])

    assert result == []
    assert _tags(db, c["id"]) == before


# ---------------------------------------------------------------------------
# contact with default empty tags
# ---------------------------------------------------------------------------

def test_bulk_add_tag_empty_tags_gets_tag(db):
    """A contact with the default tags='{}'  gets ['vip'] after the call."""
    c = _contact(db, "Empty Tags")  # tags defaults to '{}'

    _rpc(db, "vip", [c["id"]])

    assert _tags(db, c["id"]) == ["vip"]


# ===========================================================================
# CLI: crm bulk tag (Task 2.5)
# ===========================================================================

def _reg_tag(db, tag, desc="test tag"):
    """Insert a tag into the registry so the registry check passes."""
    db.table("tag_registry").insert(
        {"tag": tag, "description": desc, "created_by": "rahul"}
    ).execute()


# ---------------------------------------------------------------------------
# registry check
# ---------------------------------------------------------------------------

def test_unknown_tag_exits_1(db):
    """A tag not in tag_registry must exit 1 immediately (before any gate check)."""
    r = runner.invoke(app, ["bulk", "tag", "nonexistent_tag",
                            "--status", "contact_on_file", "--yes", "--agent", "rahul"])
    assert r.exit_code == 1, r.output
    assert "nonexistent_tag" in r.output


# ---------------------------------------------------------------------------
# happy path: chunked writes, count-clarity in JSON output
# ---------------------------------------------------------------------------

def test_bulk_tag_happy_path_json(db):
    """Register 'vip', seed 3 contacts (1 already has 'vip'), bulk tag → RPC called
    chunked at CHUNK=2; JSON shows cohort_count=3, changed_count=2, 2 ids in affected.
    """
    _reg_tag(db, "vip")
    pre = _contact(db, "Pre-Tagged", tags=["vip"])
    c1  = _contact(db, "Fresh A", connection_status="in_network")
    c2  = _contact(db, "Fresh B", connection_status="in_network")
    # pre is also in_network so the cohort matches all three
    db.table("contacts").update({"connection_status": "in_network"}).eq("id", pre["id"]).execute()

    import crm.commands.bulk as bulk_cmd
    spy = CountingClient(db)
    with patch("crm.commands.bulk.get_client", return_value=spy), \
            patch.object(bulk_cmd, "CHUNK", 2):
        r = runner.invoke(app, ["bulk", "tag", "vip",
                                "--status", "in_network", "--yes", "--json",
                                "--agent", "rahul"])

    assert r.exit_code == 0, r.output
    data = json.loads(r.output)
    assert data["dry_run"] is False
    assert data["cohort_count"] == 3
    assert data["changed_count"] == 2                  # only the two un-tagged ones
    assert len(data["affected"]) == 2
    # the pre-tagged contact must NOT appear in affected
    assert pre["id"] not in data["affected"]
    # RPC called twice (3 ids at CHUNK=2 → 2 slices)
    assert spy.rpc_count("bulk_add_tag") == 2


# ---------------------------------------------------------------------------
# dry-run: no writes
# ---------------------------------------------------------------------------

def test_bulk_tag_dry_run(db):
    """dry-run emits preview JSON with dry_run=True and no changed_count; no RPC called."""
    _reg_tag(db, "vip")
    for i in range(3):
        _contact(db, f"Dry {i}", connection_status="in_network")

    import crm.commands.bulk as bulk_cmd
    spy = CountingClient(db)
    with patch("crm.commands.bulk.get_client", return_value=spy), \
            patch.object(bulk_cmd, "CHUNK", 2):
        r = runner.invoke(app, ["bulk", "tag", "vip",
                                "--status", "in_network", "--dry-run", "--json"])

    assert r.exit_code == 0, r.output
    data = json.loads(r.output)
    assert data["dry_run"] is True
    assert data["cohort_count"] == 3
    assert "changed_count" not in data
    # no RPC fired
    assert spy.rpc_count("bulk_add_tag") == 0


# ---------------------------------------------------------------------------
# write without --yes exits 2
# ---------------------------------------------------------------------------

def test_bulk_tag_no_yes_exits_2(db):
    """Write without --yes must be refused (exit 2), same as bulk set."""
    _reg_tag(db, "vip")
    _contact(db, "No Yes", connection_status="in_network")
    r = runner.invoke(app, ["bulk", "tag", "vip", "--status", "in_network"])
    assert r.exit_code == 2, r.output
