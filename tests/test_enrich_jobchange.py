from typer.testing import CliRunner

from crm.cli import app

runner = CliRunner()


def _reg(db):
    db.table("agents").upsert({"id": "claude-web", "description": "test"}, on_conflict="id").execute()


def test_job_change_recorded_and_listed(db):
    _reg(db)
    c = db.table("contacts").insert({"full_name": "Ada", "current_company": None}).execute().data[0]
    # first company (golden), then a new one (job change old->new)
    db.rpc("enrich_apply_candidate", {
        "p_contact_id": c["id"], "p_field": "current_company", "p_value": "OldCo",
        "p_method": "enrich_api", "p_source": "pdl", "p_confidence": 0.9,
        "p_source_detail": None, "p_dry_run": False}).execute()
    db.rpc("enrich_apply_candidate", {
        "p_contact_id": c["id"], "p_field": "current_company", "p_value": "NewCo",
        "p_method": "enrich_api", "p_source": "pdl", "p_confidence": 0.95,
        "p_source_detail": None, "p_dry_run": False}).execute()

    r = runner.invoke(app, ["enrich", "changes", "--since", "2000-01-01", "--json"])
    assert r.exit_code == 0, r.output
    import json
    rows = json.loads(r.output)
    change = [x for x in rows if x["contact_id"] == c["id"] and x["field"] == "current_company"]
    assert len(change) == 1
    assert change[0]["old"] == "OldCo"
    assert change[0]["new"] == "NewCo"


def test_changes_respects_since(db):
    _reg(db)
    c = db.table("contacts").insert({"full_name": "Bob", "current_role": None}).execute().data[0]
    db.rpc("enrich_apply_candidate", {
        "p_contact_id": c["id"], "p_field": "current_role", "p_value": "Eng",
        "p_method": "enrich_api", "p_source": "pdl", "p_confidence": 0.9,
        "p_source_detail": None, "p_dry_run": False}).execute()
    db.rpc("enrich_apply_candidate", {
        "p_contact_id": c["id"], "p_field": "current_role", "p_value": "Staff Eng",
        "p_method": "enrich_api", "p_source": "pdl", "p_confidence": 0.95,
        "p_source_detail": None, "p_dry_run": False}).execute()
    # a far-future --since should exclude it
    r = runner.invoke(app, ["enrich", "changes", "--since", "2999-01-01", "--json"])
    assert r.exit_code == 0, r.output
    import json
    rows = json.loads(r.output)
    assert [x for x in rows if x["contact_id"] == c["id"]] == []
