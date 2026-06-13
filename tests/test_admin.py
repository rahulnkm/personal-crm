from typer.testing import CliRunner

from crm.cli import app

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
