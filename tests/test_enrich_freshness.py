import datetime

from typer.testing import CliRunner

from crm.cli import app

runner = CliRunner()


def _d(days):
    return (datetime.date.today() + datetime.timedelta(days=days)).isoformat()


def test_refresh_after_ttls(db):
    f = lambda field, method: db.rpc(
        "enrich_refresh_after", {"p_field": field, "p_method": method}).execute().data
    assert f("current_company", "enrich_api") == _d(90)
    assert f("current_role", "enrich_api") == _d(90)
    assert f("company_category", "enrich_api") == _d(90)
    assert f("location", "enrich_api") == _d(180)
    assert f("origin_context", "enrich_api") is None      # stable → never
    assert f("expertise", "enrich_api") is None
    assert f("current_company", "manual_set") is None     # manual → never


def test_apply_stamps_refresh_after(db):
    c = db.table("contacts").insert({"full_name": "Fresh", "current_company": None}).execute().data[0]
    db.rpc("enrich_apply_candidate", {
        "p_contact_id": c["id"], "p_field": "current_company", "p_value": "Acme",
        "p_method": "enrich_api", "p_source": "x", "p_confidence": 0.9,
        "p_source_detail": None, "p_dry_run": False}).execute()
    vol = db.table("enrichment_log").select("refresh_after").eq("contact_id", c["id"]) \
        .eq("field", "current_company").eq("is_current", True).single().execute().data
    assert vol["refresh_after"] == _d(90)
    db.rpc("enrich_apply_candidate", {
        "p_contact_id": c["id"], "p_field": "origin_context", "p_value": "met at X",
        "p_method": "enrich_api", "p_source": "x", "p_confidence": 0.9,
        "p_source_detail": None, "p_dry_run": False}).execute()
    stable = db.table("enrichment_log").select("refresh_after").eq("contact_id", c["id"]) \
        .eq("field", "origin_context").eq("is_current", True).single().execute().data
    assert stable["refresh_after"] is None


def test_due_lists_stale_excludes_fresh(db):
    stale = db.table("contacts").insert({"full_name": "Stale", "connection_status": "in_network",
                                         "closeness_tier": "t1_irl_messaging"}).execute().data[0]
    fresh = db.table("contacts").insert({"full_name": "Fresh2", "connection_status": "in_network"}).execute().data[0]
    db.table("enrichment_log").insert({"contact_id": stale["id"], "field": "current_company",
        "new_value": "A", "source": "x", "method": "enrich_api", "is_current": True,
        "refresh_after": _d(-1)}).execute()
    db.table("enrichment_log").insert({"contact_id": fresh["id"], "field": "current_company",
        "new_value": "B", "source": "x", "method": "enrich_api", "is_current": True,
        "refresh_after": _d(30)}).execute()
    r = runner.invoke(app, ["enrich", "due", "--json"])
    assert r.exit_code == 0, r.output
    assert stale["id"] in r.output
    assert fresh["id"] not in r.output
