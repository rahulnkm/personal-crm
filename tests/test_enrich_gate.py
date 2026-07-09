"""`enrich apply` validation gate — reject ungrounded/malformed agent writes at the door.

Narrative fields (origin_context, notes) and expertise elements need a real
grounding span in source_detail; expertise elements must be type:slug facets.
Rejection is per-field (outcome in the JSON result), never a hard exit.
"""
import json

import pytest
from typer.testing import CliRunner

from crm.cli import app
from crm.enrich import (
    EXPERTISE_FACET_RE,
    MIN_SOURCE_DETAIL_LEN,
    REJECTED_BAD_FACET,
    REJECTED_UNGROUNDED,
    gate_candidate,
    parse_payload,
)

runner = CliRunner()

SPAN = '"we met at the Genesis hackathon in Austin, March 2024, and built the settlement bot together"'


def _agent(db):
    db.table("agents").upsert({"id": "claude-web", "description": "test"}, on_conflict="id").execute()


def _apply(db, contact_id, payload):
    r = runner.invoke(app, ["enrich", "apply", contact_id, "--agent", "claude-web", "--json"],
                      input=json.dumps(payload))
    assert r.exit_code == 0, r.output
    return json.loads(r.stdout)


# ----- (a) span-less narrative → rejected_ungrounded, nothing written -----

def test_spanless_origin_context_rejected_and_not_written(db):
    _agent(db)
    c = db.table("contacts").insert({"full_name": "Gate A"}).execute().data[0]
    out = _apply(db, c["id"], [{"field": "origin_context", "value": "met at a hackathon",
                                "confidence": 0.9, "source": "agent:claude-web"}])
    assert out[0]["outcome"] == REJECTED_UNGROUNDED
    assert db.table("contacts").select("origin_context").eq("id", c["id"]).single().execute().data["origin_context"] is None
    assert db.table("enrichment_log").select("id").eq("contact_id", c["id"]).eq("field", "origin_context").execute().data == []


# ----- (b) length threshold is exact: 19 rejected, 20 passes -----

def test_source_detail_length_threshold(db):
    _agent(db)
    c = db.table("contacts").insert({"full_name": "Gate B"}).execute().data[0]
    assert MIN_SOURCE_DETAIL_LEN == 20
    out = _apply(db, c["id"], [{"field": "origin_context", "value": "met somewhere",
                                "confidence": 0.9, "source": "agent:claude-web",
                                "source_detail": "x" * 19}])
    assert out[0]["outcome"] == REJECTED_UNGROUNDED
    out = _apply(db, c["id"], [{"field": "origin_context", "value": "met somewhere",
                                "confidence": 0.9, "source": "agent:claude-web",
                                "source_detail": "x" * 20}])
    assert out[0]["outcome"] != REJECTED_UNGROUNDED
    assert db.table("contacts").select("origin_context").eq("id", c["id"]).single().execute().data["origin_context"] == "met somewhere"


# ----- (c) known limit: the gate checks presence+length, not span-ness -----

def test_pointer_style_detail_over_threshold_still_accepted(db):
    # LIMIT (documented): the gate cannot judge semantics — a pointer like
    # "iMessage +17325550101, 2021-2023" is >=20 chars so it passes. The 20-char
    # floor only kills empty/near-empty details; span-ness stays a review concern.
    _agent(db)
    c = db.table("contacts").insert({"full_name": "Gate C"}).execute().data[0]
    out = _apply(db, c["id"], [{"field": "notes", "value": "long-time collaborator",
                                "confidence": 0.9, "source": "agent:claude-web",
                                "source_detail": "iMessage +17325550101, 2021-2023"}])
    assert out[0]["outcome"] not in (REJECTED_UNGROUNDED, REJECTED_BAD_FACET)


def test_evidence_fold_satisfies_span_check():
    # LIMIT (pinned): parse_payload folds `evidence` into source_detail BEFORE the
    # gate runs, so a >=20-char evidence alone (a justification, not a quoted span)
    # passes the ungrounded check. Explicit, not accidental.
    cand = parse_payload(
        '{"field":"origin_context","value":"met at x","source":"a",'
        '"evidence":"profile bio mentions the hackathon"}')[0]
    assert gate_candidate(cand) is None


# ----- (d) stringified-JSON-array expertise → rejected_bad_facet naming the bug -----

def test_expertise_stringified_array_rejected_with_named_bug(db):
    _agent(db)
    c = db.table("contacts").insert({"full_name": "Gate D"}).execute().data[0]
    out = _apply(db, c["id"], [{"field": "expertise", "value": '["role:founder","domain:ai"]',
                                "confidence": 0.9, "source": "agent:claude-web",
                                "source_detail": SPAN}])
    assert out[0]["outcome"] == REJECTED_BAD_FACET
    assert "array" in out[0]["error"].lower()  # names the stringified-JSON-array bug
    assert db.table("contacts").select("expertise").eq("id", c["id"]).single().execute().data["expertise"] in (None, [])


# ----- (e) facet shape: type:slug only -----

def test_expertise_facet_shape(db):
    _agent(db)
    c = db.table("contacts").insert({"full_name": "Gate E"}).execute().data[0]
    out = _apply(db, c["id"], [{"field": "expertise", "value": "skill:RF circuit design",
                                "confidence": 0.9, "source": "agent:claude-web",
                                "source_detail": SPAN}])
    assert out[0]["outcome"] == REJECTED_BAD_FACET
    out = _apply(db, c["id"], [{"field": "expertise", "value": "skill:rf-circuit-design",
                                "confidence": 0.9, "source": "agent:claude-web",
                                "source_detail": SPAN}])
    assert out[0]["outcome"] == "added"
    got = db.table("contacts").select("expertise").eq("id", c["id"]).single().execute().data["expertise"]
    assert got == ["skill:rf-circuit-design"]


# ----- (f) per-field skip: bad narrative + good scalar in one payload -----

def test_mixed_payload_scalar_applies_narrative_rejected(db):
    _agent(db)
    c = db.table("contacts").insert({"full_name": "Gate F", "current_company": None}).execute().data[0]
    out = _apply(db, c["id"], [
        {"field": "notes", "value": "great person", "confidence": 0.9, "source": "agent:claude-web"},
        {"field": "current_company", "value": "OrbitalWorks", "confidence": 0.9, "source": "agent:claude-web"},
    ])
    by_field = {r["field"]: r for r in out}
    assert by_field["notes"]["outcome"] == REJECTED_UNGROUNDED
    assert by_field["current_company"]["outcome"] == "golden"
    assert db.table("contacts").select("current_company").eq("id", c["id"]).single().execute().data["current_company"] == "OrbitalWorks"


# ----- (g) well-formed payload end-to-end unchanged vs today -----

def test_grounded_payload_unchanged_end_to_end(db):
    _agent(db)
    c = db.table("contacts").insert({"full_name": "Gate G"}).execute().data[0]
    out = _apply(db, c["id"], [
        {"field": "origin_context", "value": "met at Genesis hackathon, Austin 2024",
         "confidence": 0.9, "source": "agent:claude-web", "source_detail": SPAN},
        {"field": "expertise", "value": "domain:settlement-infra",
         "confidence": 0.9, "source": "agent:claude-web", "source_detail": SPAN},
        {"field": "current_company", "value": "Acme", "confidence": 0.9, "source": "agent:claude-web"},
    ])
    outcomes = {r["field"]: r["outcome"] for r in out}
    assert outcomes == {"origin_context": "golden", "expertise": "added", "current_company": "golden"}
    got = db.table("contacts").select("origin_context,expertise,current_company").eq("id", c["id"]).single().execute().data
    assert got["origin_context"] == "met at Genesis hackathon, Austin 2024"
    assert got["expertise"] == ["domain:settlement-infra"]
    assert got["current_company"] == "Acme"


# ----- pure helper: error text teaches the span distinction; regex boundaries -----

def test_gate_error_text_teaches_span_vs_pointer():
    cand = parse_payload('{"field":"origin_context","value":"met at x","source":"a"}')[0]
    outcome, msg = gate_candidate(cand)
    assert outcome == REJECTED_UNGROUNDED
    assert "phone number + date range is not a span" in msg


def test_list_valued_candidate_rejected_at_parse():
    # Agents naturally emit the whole expertise array as one candidate's `value`.
    # That must fail cleanly at parse (teaching one-facet-per-candidate), not crash
    # later in gate_candidate with 'list' object has no attribute 'strip'.
    with pytest.raises(ValueError, match="one facet per candidate"):
        parse_payload(
            '[{"field":"expertise","value":["skill:zk","tool:circom"],'
            '"source":"a","source_detail":"her msg 2021-03-14: pushed the circom circuits"}]')


def test_expertise_regex_boundaries():
    assert EXPERTISE_FACET_RE.match("tool:figma")
    assert EXPERTISE_FACET_RE.match("domain:ai-agents-2")
    assert not EXPERTISE_FACET_RE.match("vibe:cool")       # unknown facet type
    assert not EXPERTISE_FACET_RE.match("role:Founder")    # uppercase
    assert not EXPERTISE_FACET_RE.match("role:")           # empty slug
    assert not EXPERTISE_FACET_RE.match(" role:founder")   # leading space
