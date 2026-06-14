from typer.testing import CliRunner

from crm.cli import app

runner = CliRunner()


def _review_row(db, cid, field="current_role", value="Wizard", source="x", conf=0.5,
                reason="low_confidence"):
    return db.table("enrich_review").insert({
        "contact_id": cid, "field": field, "candidate_value": value,
        "source": source, "confidence": conf, "reason": reason}).execute().data[0]


def test_review_list_shows_open(db):
    c = db.table("contacts").insert({"full_name": "Ada"}).execute().data[0]
    _review_row(db, c["id"])
    r = runner.invoke(app, ["enrich", "review", "--json"])
    assert r.exit_code == 0, r.output
    assert "Wizard" in r.output


def test_review_approve_writes_manual_and_resolves(db):
    c = db.table("contacts").insert({"full_name": "Ada", "current_role": None}).execute().data[0]
    row = _review_row(db, c["id"], field="current_role", value="Engineer")
    r = runner.invoke(app, ["enrich", "review", "--approve", row["id"]])
    assert r.exit_code == 0, r.output
    got = db.table("contacts").select("current_role").eq("id", c["id"]).single().execute().data
    assert got["current_role"] == "Engineer"
    cur = (db.table("enrichment_log").select("method,is_current")
           .eq("contact_id", c["id"]).eq("field", "current_role").eq("is_current", True).execute().data)
    assert len(cur) == 1 and cur[0]["method"] == "manual_set"
    resolved = db.table("enrich_review").select("status").eq("id", row["id"]).single().execute().data
    assert resolved["status"] == "resolved"


def test_review_reject_tombstones_and_sticks(db):
    c = db.table("contacts").insert({"full_name": "Bob", "current_company": None}).execute().data[0]
    row = _review_row(db, c["id"], field="current_company", value="BrokerCo")
    r = runner.invoke(app, ["enrich", "review", "--reject", row["id"]])
    assert r.exit_code == 0, r.output
    resolved = db.table("enrich_review").select("status").eq("id", row["id"]).single().execute().data
    assert resolved["status"] == "resolved"
    # re-applying the rejected value later still loses
    out = db.rpc("enrich_apply_candidate", {
        "p_contact_id": c["id"], "p_field": "current_company", "p_value": "BrokerCo",
        "p_method": "enrich_api", "p_source": "pdl", "p_confidence": 0.99,
        "p_source_detail": None, "p_dry_run": False}).execute().data
    assert out == "losing"
    got = db.table("contacts").select("current_company").eq("id", c["id"]).single().execute().data
    assert got["current_company"] is None


def test_review_skip_marks_skipped(db):
    c = db.table("contacts").insert({"full_name": "Cy"}).execute().data[0]
    row = _review_row(db, c["id"])
    r = runner.invoke(app, ["enrich", "review", "--skip", row["id"]])
    assert r.exit_code == 0, r.output
    got = db.table("enrich_review").select("status").eq("id", row["id"]).single().execute().data
    assert got["status"] == "skipped"
