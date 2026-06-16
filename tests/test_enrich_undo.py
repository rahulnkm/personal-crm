import json

from typer.testing import CliRunner

from crm.cli import app

runner = CliRunner()


def _reg(db):
    db.table("agents").upsert({"id": "claude-web", "description": "test"}, on_conflict="id").execute()


def _apply(db, cid, field, value, source, conf):
    db.rpc("enrich_apply_candidate", {
        "p_contact_id": cid, "p_field": field, "p_value": value,
        "p_method": "enrich_api", "p_source": source, "p_confidence": conf,
        "p_source_detail": None, "p_dry_run": False}).execute()


def test_undo_reelects_prior_winner(db):
    _reg(db)
    c = db.table("contacts").insert({"full_name": "Ada", "location": None}).execute().data[0]
    _apply(db, c["id"], "location", "NYC", "gravatar", 0.8)
    _apply(db, c["id"], "location", "SF", "pdl", 0.95)  # SF is now current
    assert db.table("contacts").select("location").eq("id", c["id"]).single().execute().data["location"] == "SF"
    r = runner.invoke(app, ["enrich", "undo", c["id"], "location", "--agent", "claude-web"])
    assert r.exit_code == 0, r.output
    got = db.table("contacts").select("location").eq("id", c["id"]).single().execute().data
    assert got["location"] == "NYC"  # prior winner re-elected


def test_forget_redacts_values_keeps_rows(db):
    _reg(db)
    c = db.table("contacts").insert({"full_name": "Bob", "location": None}).execute().data[0]
    _apply(db, c["id"], "location", "NYC", "gravatar", 0.8)
    n_before = len(db.table("enrichment_log").select("id").eq("contact_id", c["id"]).execute().data)
    r = runner.invoke(app, ["enrich", "forget", c["id"], "--agent", "claude-web"])
    assert r.exit_code == 0, r.output
    rows = db.table("enrichment_log").select("old_value,new_value,redacted_at").eq("contact_id", c["id"]).execute().data
    assert len(rows) == n_before  # structural rows kept
    assert all(x["new_value"] is None and x["old_value"] is None for x in rows)
    assert all(x["redacted_at"] is not None for x in rows)


def test_enrich_stats_counts(db):
    _reg(db)
    c = db.table("contacts").insert({"full_name": "Cy", "location": None}).execute().data[0]
    _apply(db, c["id"], "location", "NYC", "gravatar", 0.8)  # golden
    _apply(db, c["id"], "current_role", "Wizard", "x", 0.4)  # review (low conf)
    r = runner.invoke(app, ["enrich", "stats", "--json"])
    assert r.exit_code == 0, r.output
    rows = json.loads(r.output)
    metrics = {x["metric"]: x["count"] for x in rows}
    assert metrics.get("in_review", 0) >= 1
    assert any(m.startswith("current_by_source=") for m in metrics)
