"""Bulk policy command: promote contacts with a real touchpoint tier to in_network.

Policy (Rahul's call 6-13): anyone with a t1 (IRL/messaging) or t2 (platform DM)
touchpoint is a real connection, so their connection_status flips to in_network.
The command is additive and idempotent — it only PROMOTES contact_on_file rows and
never demotes a manually-set in_network.
"""
import json

from typer.testing import CliRunner

from crm.cli import app

runner = CliRunner()


def _seed(db, name, status, tier):
    return db.table("contacts").insert(
        {"full_name": name, "connection_status": status, "closeness_tier": tier}
    ).execute().data[0]


def _status(db, cid):
    return db.table("contacts").select("connection_status").eq("id", cid) \
        .single().execute().data["connection_status"]


def test_promotes_t1_and_t2_not_none(db):
    t1 = _seed(db, "T1 Person", "contact_on_file", "t1_irl_messaging")
    t2 = _seed(db, "T2 Person", "contact_on_file", "t2_dm")
    nun = _seed(db, "No Touch", "contact_on_file", "none")
    r = runner.invoke(app, ["sync-status", "--json"])
    assert r.exit_code == 0, r.output
    assert json.loads(r.output)["promoted"] == 2
    assert _status(db, t1["id"]) == "in_network"
    assert _status(db, t2["id"]) == "in_network"
    assert _status(db, nun["id"]) == "contact_on_file"


def test_idempotent_second_run_promotes_zero(db):
    _seed(db, "T1 Person", "contact_on_file", "t1_irl_messaging")
    runner.invoke(app, ["sync-status"])
    r = runner.invoke(app, ["sync-status", "--json"])
    assert json.loads(r.output)["promoted"] == 0


def test_never_demotes_manual_in_network(db):
    # someone manually marked a real connection despite no touchpoint tier
    c = _seed(db, "Manual In", "in_network", "none")
    runner.invoke(app, ["sync-status"])
    assert _status(db, c["id"]) == "in_network"


def test_dry_run_reports_but_does_not_write(db):
    c = _seed(db, "T1 Person", "contact_on_file", "t1_irl_messaging")
    r = runner.invoke(app, ["sync-status", "--dry-run", "--json"])
    assert json.loads(r.output)["promoted"] == 1
    assert _status(db, c["id"]) == "contact_on_file"  # unchanged


def test_custom_tier_restricts_scope(db):
    t1 = _seed(db, "T1 Person", "contact_on_file", "t1_irl_messaging")
    t2 = _seed(db, "T2 Person", "contact_on_file", "t2_dm")
    r = runner.invoke(app, ["sync-status", "--tier", "t1_irl_messaging", "--json"])
    assert json.loads(r.output)["promoted"] == 1
    assert _status(db, t1["id"]) == "in_network"
    assert _status(db, t2["id"]) == "contact_on_file"


def test_invalid_tier_fails_cleanly(db):
    r = runner.invoke(app, ["sync-status", "--tier", "bogus"])
    assert r.exit_code == 1
