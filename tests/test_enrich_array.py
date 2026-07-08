"""Array-field write-path: set-union RPC + routing.

Scalar attributes go through enrich_apply_candidate (survivorship). ARRAY fields
(expertise/interests/tags/affiliations) have no single winner — they accumulate
via enrich_apply_array (set-union, per-element provenance, tombstone-aware).
"""
from typer.testing import CliRunner

from crm.cli import app
from crm.enrich import ARRAY_FIELDS

runner = CliRunner()


def _arr(db, contact_id, field):
    return db.table("contacts").select(field).eq("id", contact_id).single().execute().data[field]


def _logs(db, contact_id, field, value):
    return (db.table("enrichment_log").select("*")
            .eq("contact_id", contact_id).eq("field", field).eq("new_value", value)
            .execute().data)


def test_array_fields_constant():
    assert ARRAY_FIELDS == {"tags", "affiliations", "expertise", "interests"}


def test_rpc_adds_element_and_writes_provenance(db):
    c = db.table("contacts").insert({"full_name": "Ari"}).execute().data[0]
    out = db.rpc("enrich_apply_array", {
        "p_contact_id": c["id"], "p_field": "expertise", "p_value": "domain:web3",
        "p_method": "enrich_agent", "p_source": "agent:x", "p_confidence": 0.9,
    }).execute().data
    assert out == "added"
    assert _arr(db, c["id"], "expertise") == ["domain:web3"]
    logs = _logs(db, c["id"], "expertise", "domain:web3")
    assert len(logs) == 1
    assert logs[0]["is_current"] is False  # arrays have no single winner


def test_rpc_idempotent_no_duplicate(db):
    c = db.table("contacts").insert({"full_name": "Bea"}).execute().data[0]
    args = {"p_contact_id": c["id"], "p_field": "interests", "p_value": "ai",
            "p_method": "enrich_agent", "p_source": "agent:x", "p_confidence": 0.9}
    assert db.rpc("enrich_apply_array", args).execute().data == "added"
    assert db.rpc("enrich_apply_array", args).execute().data == "already"
    assert _arr(db, c["id"], "interests") == ["ai"]
    assert len(_logs(db, c["id"], "interests", "ai")) == 1  # no second provenance row


def test_rpc_dry_run_does_not_mutate(db):
    c = db.table("contacts").insert({"full_name": "Cy"}).execute().data[0]
    out = db.rpc("enrich_apply_array", {
        "p_contact_id": c["id"], "p_field": "tags", "p_value": "vc",
        "p_method": "enrich_agent", "p_source": "agent:x", "p_confidence": 0.9,
        "p_dry_run": True}).execute().data
    assert out == "added"  # would-be outcome
    assert _arr(db, c["id"], "tags") == []
    assert _logs(db, c["id"], "tags", "vc") == []


def test_rpc_blank_value_noop(db):
    c = db.table("contacts").insert({"full_name": "Di"}).execute().data[0]
    out = db.rpc("enrich_apply_array", {
        "p_contact_id": c["id"], "p_field": "tags", "p_value": "  ",
        "p_method": "enrich_agent", "p_source": "agent:x", "p_confidence": 0.9}).execute().data
    assert out == "noop"
    assert _arr(db, c["id"], "tags") == []


def test_rpc_rejects_non_array_field(db):
    c = db.table("contacts").insert({"full_name": "Eve"}).execute().data[0]
    import pytest
    with pytest.raises(Exception):
        db.rpc("enrich_apply_array", {
            "p_contact_id": c["id"], "p_field": "location", "p_value": "SF",
            "p_method": "enrich_agent", "p_source": "x", "p_confidence": 0.9}).execute()


def test_reject_removes_element_and_tombstones(db):
    c = db.table("contacts").insert({"full_name": "Fi"}).execute().data[0]
    args = {"p_contact_id": c["id"], "p_field": "expertise", "p_value": "domain:nft",
            "p_method": "enrich_agent", "p_source": "agent:x", "p_confidence": 0.9}
    db.rpc("enrich_apply_array", args).execute()
    assert _arr(db, c["id"], "expertise") == ["domain:nft"]

    db.rpc("enrich_reject_array", {
        "p_contact_id": c["id"], "p_field": "expertise", "p_value": "domain:nft"}).execute()
    assert _arr(db, c["id"], "expertise") == []  # removed from array
    disputed = (db.table("enrichment_log").select("*")
                .eq("contact_id", c["id"]).eq("field", "expertise")
                .eq("new_value", "domain:nft").eq("verification_status", "disputed")
                .execute().data)
    assert len(disputed) == 1

    # subsequent apply of the tombstoned value → tombstoned, not added
    assert db.rpc("enrich_apply_array", args).execute().data == "tombstoned"
    assert _arr(db, c["id"], "expertise") == []


def test_cli_enrich_apply_routes_array_field(db):
    db.table("agents").upsert({"id": "claude-web", "description": "t"}, on_conflict="id").execute()
    c = db.table("contacts").insert({"full_name": "Gus"}).execute().data[0]
    r = runner.invoke(app, ["enrich", "apply", c["id"], "--agent", "claude-web", "--json"],
                      input='{"field":"expertise","value":"role:investor","confidence":0.9,"source":"agent:claude-web",'
                            '"source_detail":"\\"led the seed round in our Jan 2026 call\\""}')
    assert r.exit_code == 0, r.output
    assert "added" in r.output
    assert _arr(db, c["id"], "expertise") == ["role:investor"]


def test_cli_set_routes_array_via_rpc(db):
    c = db.table("contacts").insert({"full_name": "Hal"}).execute().data[0]
    r = runner.invoke(app, ["set", c["id"], "expertise=domain:web3", "--agent", "rahul"])
    assert r.exit_code == 0, r.output
    assert _arr(db, c["id"], "expertise") == ["domain:web3"]
    logs = _logs(db, c["id"], "expertise", "domain:web3")
    assert len(logs) == 1
    assert logs[0]["method"] == "manual_set"


def test_cli_review_reject_array_field(db):
    db.table("agents").upsert({"id": "claude-web", "description": "t"}, on_conflict="id").execute()
    c = db.table("contacts").insert({"full_name": "Ivy", "expertise": ["domain:defi"]}).execute().data[0]
    # an open review item on an array field
    rid = db.table("enrich_review").insert({
        "contact_id": c["id"], "field": "expertise", "candidate_value": "domain:defi",
        "source": "agent:x", "confidence": 0.9, "reason": "low_confidence"}).execute().data[0]["id"]
    r = runner.invoke(app, ["enrich", "review", "--reject", rid, "--agent", "claude-web"])
    assert r.exit_code == 0, r.output
    assert _arr(db, c["id"], "expertise") == []  # removed via array reject
    disputed = (db.table("enrichment_log").select("id")
                .eq("contact_id", c["id"]).eq("field", "expertise")
                .eq("verification_status", "disputed").execute().data)
    assert len(disputed) == 1
