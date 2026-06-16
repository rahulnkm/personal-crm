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


def test_parse_aliases_natural_field_names():
    # agents naturally send "company"/"role"; alias to the real golden columns
    assert parse_payload('{"field":"company","value":"Bevy","source":"x"}')[0].field == "current_company"
    assert parse_payload('{"field":"role","value":"PM","source":"x"}')[0].field == "current_role"
    assert parse_payload('{"field":"title","value":"CEO","source":"x"}')[0].field == "current_role"


def test_apply_company_alias_writes_current_company(db):
    # regression: real (non-dry-run) write of the "company" field used to crash the
    # survivorship RPC with 22004 (null col_type for a non-existent "company" column).
    db.table("agents").upsert({"id": "claude-web", "description": "test"}, on_conflict="id").execute()
    c = db.table("contacts").insert({"full_name": "Bevy Person", "current_company": None}).execute().data[0]
    r = runner.invoke(app, ["enrich", "apply", c["id"], "--agent", "claude-web", "--json"],
                      input='[{"field":"company","value":"Bevy","confidence":0.7,"source":"agent:claude-web"}]')
    assert r.exit_code == 0, r.output
    assert db.table("contacts").select("current_company").eq("id", c["id"]).single().execute().data["current_company"] == "Bevy"


def test_apply_array_field_routes_to_array_rpc(db):
    # an attribute that is an ARRAY field must land in the array column via
    # enrich_apply_array (set-union), not silently no-op on the scalar RPC.
    db.table("agents").upsert({"id": "claude-web", "description": "test"}, on_conflict="id").execute()
    c = db.table("contacts").insert({"full_name": "Exp Person"}).execute().data[0]
    r = runner.invoke(app, ["enrich", "apply", c["id"], "--agent", "claude-web", "--json"],
                      input='{"field":"expertise","value":"role:investor","confidence":0.9,"source":"agent:claude-web"}')
    assert r.exit_code == 0, r.output
    assert "added" in r.output
    got = db.table("contacts").select("expertise").eq("id", c["id"]).single().execute().data["expertise"]
    assert got == ["role:investor"]


def test_apply_unknown_field_fails_cleanly(db):
    # a genuinely unknown field must fail with a clear message, not a raw 22004
    db.table("agents").upsert({"id": "claude-web", "description": "test"}, on_conflict="id").execute()
    c = db.table("contacts").insert({"full_name": "Zed"}).execute().data[0]
    r = runner.invoke(app, ["enrich", "apply", c["id"], "--agent", "claude-web", "--json"],
                      input='[{"field":"definitely_not_a_column","value":"x","confidence":0.9,"source":"x"}]')
    surfaced = (r.output + str(r.exception or "")).lower()
    assert r.exit_code != 0
    assert "unknown contacts field" in surfaced  # clean message…
    assert "22004" not in surfaced               # …not the cryptic identifier crash
