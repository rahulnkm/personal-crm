from typer.testing import CliRunner

from crm.cli import app

runner = CliRunner()


def test_set_scalar_writes_manual_provenance(db):
    c = db.table("contacts").insert({"full_name": "Ada"}).execute().data[0]
    r = runner.invoke(app, ["set", c["id"], "current_company=Analytical"])
    assert r.exit_code == 0, r.output
    got = db.table("contacts").select("current_company").eq("id", c["id"]).single().execute().data
    assert got["current_company"] == "Analytical"
    rows = (db.table("enrichment_log").select("method,is_current,new_value")
            .eq("contact_id", c["id"]).eq("field", "current_company").execute().data)
    cur = [row for row in rows if row["is_current"]]
    assert len(cur) == 1
    assert cur[0]["method"] == "manual_set"
    assert cur[0]["new_value"] == "Analytical"


def test_set_manual_blocks_subsequent_enrich(db):
    c = db.table("contacts").insert({"full_name": "Bob"}).execute().data[0]
    runner.invoke(app, ["set", c["id"], "current_company=RealCo"])
    out = db.rpc("enrich_apply_candidate", {
        "p_contact_id": c["id"], "p_field": "current_company", "p_value": "BrokerCo",
        "p_method": "enrich_api", "p_source": "pdl", "p_confidence": 0.95,
        "p_source_detail": None, "p_dry_run": False}).execute().data
    assert out in ("review", "losing")
    got = db.table("contacts").select("current_company").eq("id", c["id"]).single().execute().data
    assert got["current_company"] == "RealCo"


def test_set_blank_is_deliberate_null(db):
    c = db.table("contacts").insert({"full_name": "Cy", "location": "SF"}).execute().data[0]
    # seed provenance for the existing value so we have an is_current row
    db.rpc("enrich_seed_provenance", {}).execute()
    r = runner.invoke(app, ["set", c["id"], "location="])
    assert r.exit_code == 0, r.output
    got = db.table("contacts").select("location").eq("id", c["id"]).single().execute().data
    assert got["location"] is None


def test_set_array_keeps_union_path(db):
    c = db.table("contacts").insert({"full_name": "Di", "affiliations": ["a"]}).execute().data[0]
    r = runner.invoke(app, ["set", c["id"], "affiliations=b"])
    assert r.exit_code == 0, r.output
    got = db.table("contacts").select("affiliations").eq("id", c["id"]).single().execute().data
    assert set(got["affiliations"]) == {"a", "b"}
