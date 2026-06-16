import json
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from crm.cli import app
from tests._spy import CountingClient

runner = CliRunner()


def test_agent_register_and_list(db):
    r = runner.invoke(app, ["agent", "register", "hiring-agent",
                            "--desc", "finds people for the remote-crypto job hunt"])
    assert r.exit_code == 0
    r = runner.invoke(app, ["agent", "list", "--json"])
    assert r.exit_code == 0
    assert "hiring-agent" in r.output


def test_tags_add_requires_registered_agent(db):
    r = runner.invoke(app, ["tags", "add", "fundraising",
                            "--desc", "actively raising a round",
                            "--agent", "ghost-agent"])
    assert r.exit_code == 1  # unregistered agent → clean error, not a traceback


# default --agent is 'rahul', which exists ONLY because the migration seeds it
# and conftest deliberately does not truncate the agents table
def test_tags_add_and_list(db):
    runner.invoke(app, ["tags", "add", "fundraising", "--desc", "actively raising a round"])
    r = runner.invoke(app, ["tags", "list", "--json"])
    assert "fundraising" in r.output


def test_stats_runs(db):
    r = runner.invoke(app, ["stats"])
    assert r.exit_code == 0


def test_stats_counts_staging_interactions(db):
    db.table("staging_interactions").insert(
        {"source": "s", "source_external_id": "x", "kind": "message",
         "channel": "email", "email": "a@b.co"}).execute()
    r = runner.invoke(app, ["stats", "--json"])
    assert "touchpoints=pending" in r.output


# ---------------------------------------------------------------------------
# Task 1.3: crm_stats() RPC parity tests
# ---------------------------------------------------------------------------

def test_stats_parity_known_distribution(db):
    """Seed a known distribution; assert --json output matches the exact legacy list."""
    # 2 in_network + 1 contact_on_file; 1 t1 + 1 t2 + 1 none
    db.table("contacts").insert([
        {"full_name": "A", "connection_status": "in_network",
         "closeness_tier": "t1_irl_messaging"},
        {"full_name": "B", "connection_status": "in_network",
         "closeness_tier": "t2_dm"},
        {"full_name": "C", "connection_status": "contact_on_file",
         "closeness_tier": "none"},
    ]).execute()

    # staging: 2 pending + 1 needs_review; auto_matched / merged / rejected absent
    db.table("staging").insert([
        {"source": "s", "source_external_id": "s1",
         "full_name": "P1", "match_status": "pending"},
        {"source": "s", "source_external_id": "s2",
         "full_name": "P2", "match_status": "pending"},
        {"source": "s", "source_external_id": "s3",
         "full_name": "P3", "match_status": "needs_review"},
    ]).execute()

    # staging_interactions: 1 linked; pending + orphaned absent
    db.table("staging_interactions").insert([
        {"source": "s", "source_external_id": "si1", "kind": "email",
         "match_status": "linked"},
    ]).execute()

    r = runner.invoke(app, ["stats", "--json"])
    assert r.exit_code == 0, r.output
    out = json.loads(r.output)

    # Build expected list exactly as the legacy code would — zero-count buckets
    # dropped EXCEPT contacts_total.
    expected = [
        {"metric": "connection_status=in_network", "count": 2},
        {"metric": "connection_status=contact_on_file", "count": 1},
        {"metric": "closeness_tier=t1_irl_messaging", "count": 1},
        {"metric": "closeness_tier=t2_dm", "count": 1},
        # t3_community=0, t4_public=0 → dropped
        {"metric": "closeness_tier=none", "count": 1},
        {"metric": "staging=pending", "count": 2},
        # auto_matched=0 → dropped
        {"metric": "staging=needs_review", "count": 1},
        # merged=0, rejected=0 → dropped
        # touchpoints=pending=0, orphaned=0 → dropped
        {"metric": "touchpoints=linked", "count": 1},
        {"metric": "contacts_total", "count": 3},
    ]
    assert out == expected


def test_stats_empty_db(db):
    """With no contacts/staging rows the only output is contacts_total=0."""
    r = runner.invoke(app, ["stats", "--json"])
    assert r.exit_code == 0, r.output
    out = json.loads(r.output)
    assert out == [{"metric": "contacts_total", "count": 0}]


def test_stats_single_rpc_no_table_selects(db):
    """Round-trip spy: exactly one crm_stats RPC, zero contacts/staging selects."""
    spy = CountingClient(db)
    with patch("crm.commands.admin.get_client", return_value=spy):
        r = runner.invoke(app, ["stats", "--json"])
    assert r.exit_code == 0, r.output

    assert spy.rpc_count("crm_stats") == 1, "expected exactly one crm_stats RPC call"
    assert spy.count("contacts", "select") == 0, "expected zero contacts table selects"
    assert spy.count("staging", "select") == 0, "expected zero staging table selects"
    assert spy.count("staging_interactions", "select") == 0, \
        "expected zero staging_interactions table selects"


def test_stats_counts_are_ints(db):
    """Rendered JSON counts must be Python ints (3, not 3.0)."""
    db.table("contacts").insert([
        {"full_name": "X"}, {"full_name": "Y"}, {"full_name": "Z"},
    ]).execute()

    r = runner.invoke(app, ["stats", "--json"])
    assert r.exit_code == 0, r.output
    out = json.loads(r.output)

    for row in out:
        assert isinstance(row["count"], int), \
            f"count for {row['metric']} is {type(row['count'])}, not int"
