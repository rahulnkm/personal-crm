from typer.testing import CliRunner

from crm.cli import app
from crm.enrich import parse_payload, EnrichCandidate, ATTRIBUTE, IDENTIFIER

runner = CliRunner()


def test_parse_single_object():
    cs = parse_payload('{"field":"location","value":"SF","confidence":0.9,"source":"gravatar"}')
    assert len(cs) == 1 and cs[0].field == "location" and cs[0].kind == ATTRIBUTE


def test_parse_array_and_identifier_kind():
    cs = parse_payload('[{"field":"email","value":"a@b.com","kind":"identifier","confidence":0.9,"source":"gravatar"}]')
    assert cs[0].kind == IDENTIFIER


def test_confidence_validated_range():
    import pytest
    with pytest.raises(ValueError):
        parse_payload('{"field":"location","value":"SF","confidence":1.5,"source":"x"}')


def test_identifier_kind_inferred_from_field():
    cs = parse_payload('{"field":"email","value":"a@b.com","confidence":0.9,"source":"x"}')
    assert cs[0].kind == IDENTIFIER


def test_evidence_folded_into_source_detail():
    cs = parse_payload(
        '{"field":"location","value":"SF","confidence":0.9,"source":"x",'
        '"source_detail":"http://e.com","evidence":"profile says SF"}')
    assert cs[0].source_detail == "http://e.com · profile says SF"


def test_apply_attribute_fills_field(db):
    db.table("agents").upsert({"id": "claude-web", "description": "test"}, on_conflict="id").execute()
    c = db.table("contacts").insert({"full_name": "Ada", "current_company": None}).execute().data[0]
    r = runner.invoke(app, ["enrich", "apply", c["id"], "--agent", "claude-web", "--json"],
                      input='{"field":"current_company","value":"AnalyticEngine","confidence":0.9,"source":"agent:claude-web"}')
    assert r.exit_code == 0, r.output
    assert db.table("contacts").select("current_company").eq("id", c["id"]).single().execute().data["current_company"] == "AnalyticEngine"


def test_apply_dry_run_does_not_mutate(db):
    db.table("agents").upsert({"id": "claude-web", "description": "test"}, on_conflict="id").execute()
    c = db.table("contacts").insert({"full_name": "Bob", "location": None}).execute().data[0]
    r = runner.invoke(app, ["enrich", "apply", c["id"], "--agent", "claude-web", "--dry-run", "--json"],
                      input='{"field":"location","value":"SF","confidence":0.9,"source":"x"}')
    assert r.exit_code == 0, r.output
    assert db.table("contacts").select("location").eq("id", c["id"]).single().execute().data["location"] is None


def test_apply_low_confidence_goes_to_review(db):
    db.table("agents").upsert({"id": "claude-web", "description": "test"}, on_conflict="id").execute()
    c = db.table("contacts").insert({"full_name": "Cy", "current_role": None}).execute().data[0]
    r = runner.invoke(app, ["enrich", "apply", c["id"], "--agent", "claude-web", "--json"],
                      input='{"field":"current_role","value":"Wizard","confidence":0.5,"source":"x"}')
    assert r.exit_code == 0, r.output
    assert "review" in r.output
    assert len(db.table("enrich_review").select("id").eq("contact_id", c["id"]).execute().data) == 1
