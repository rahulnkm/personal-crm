"""Phase-1 retrieval: crm list filters, crm capsules, crm find."""
import json

from typer.testing import CliRunner

from crm.cli import app

runner = CliRunner()


def _seed(db):
    """A small, role/company-category/location-varied set with one interaction."""
    rows = db.table("contacts").insert([
        {"full_name": "Ada Founder", "current_role": "Co-Founder & CEO",
         "current_company": "AcmeSec", "company_category": "cybersecurity",
         "location": "New York, NY", "notes": "Met at a security conference. Sharp on detection.",
         "closeness_tier": "t1_irl_messaging", "connection_status": "in_network"},
        {"full_name": "Ben Engineer", "current_role": "Staff Engineer",
         "current_company": "DataCorp", "company_category": "data infrastructure",
         "location": "San Francisco, CA", "notes": "Backend wizard.",
         "closeness_tier": "t2_dm", "connection_status": "contact_on_file"},
        {"full_name": "Cleo Chief", "current_role": "Chief Executive Officer",
         "current_company": "FinTechCo", "company_category": "fintech",
         "location": "London, UK", "notes": "Runs a payments startup.",
         "closeness_tier": "none", "connection_status": "contact_on_file"},
    ]).execute().data
    by = {r["full_name"]: r for r in rows}
    db.table("interactions").insert({
        "contact_id": by["Ada Founder"]["id"], "kind": "meeting", "channel": "irl",
        "occurred_at": "2026-05-01", "summary": "Talked threat detection at RSA.",
        "logged_by": "rahul",
    }).execute()
    return by


# ----- Task 13: crm list filters -----

def test_list_role_substring_case_insensitive(db):
    _seed(db)
    r = runner.invoke(app, ["list", "--role", "founder", "--json"])
    assert r.exit_code == 0, r.output
    names = {row["full_name"] for row in json.loads(r.output)}
    assert names == {"Ada Founder"}


def test_list_role_synonym_expands_ceo(db):
    _seed(db)
    # "ceo" should also match "Chief Executive Officer" and "Co-Founder & CEO"
    r = runner.invoke(app, ["list", "--role", "ceo", "--json"])
    assert r.exit_code == 0, r.output
    names = {row["full_name"] for row in json.loads(r.output)}
    assert names == {"Ada Founder", "Cleo Chief"}


def test_list_role_comma_multi(db):
    _seed(db)
    r = runner.invoke(app, ["list", "--role", "founder,engineer", "--json"])
    assert r.exit_code == 0, r.output
    names = {row["full_name"] for row in json.loads(r.output)}
    assert names == {"Ada Founder", "Ben Engineer"}


def test_list_role_class_founder_alias(db):
    _seed(db)
    r = runner.invoke(app, ["list", "--role-class", "founder", "--json"])
    assert r.exit_code == 0, r.output
    names = {row["full_name"] for row in json.loads(r.output)}
    assert names == {"Ada Founder"}


def test_list_company_category(db):
    _seed(db)
    r = runner.invoke(app, ["list", "--company-category", "cyber", "--json"])
    assert r.exit_code == 0, r.output
    names = {row["full_name"] for row in json.loads(r.output)}
    assert names == {"Ada Founder"}


def test_list_location_substring(db):
    _seed(db)
    r = runner.invoke(app, ["list", "--location", "new york", "--json"])
    assert r.exit_code == 0, r.output
    names = {row["full_name"] for row in json.loads(r.output)}
    assert names == {"Ada Founder"}


def test_list_filters_compose_with_status(db):
    _seed(db)
    r = runner.invoke(app, ["list", "--role", "ceo", "--status", "in_network", "--json"])
    assert r.exit_code == 0, r.output
    names = {row["full_name"] for row in json.loads(r.output)}
    assert names == {"Ada Founder"}  # Cleo is a CEO but not in_network


def test_list_empty_result_is_exit_zero_empty_json(db):
    _seed(db)
    r = runner.invoke(app, ["list", "--company-category", "no-such-thing", "--json"])
    assert r.exit_code == 0, r.output
    assert json.loads(r.output) == []


# ----- Task 14: crm capsules -----

def test_capsules_shape(db):
    by = _seed(db)
    r = runner.invoke(app, ["capsules", "--json"])
    assert r.exit_code == 0, r.output
    caps = json.loads(r.output)
    ada = next(c for c in caps if c["name"] == "Ada Founder")
    # required compact keys present
    for k in ("name", "role", "company", "company_category", "expertise", "tags",
              "note", "topics", "location", "tier", "last", "stale"):
        assert k in ada, f"missing key {k}"
    assert ada["role"] == "Co-Founder & CEO"
    assert ada["company"] == "AcmeSec"
    assert ada["company_category"] == "cybersecurity"
    assert ada["location"] == "New York, NY"
    assert ada["tier"] == "t1_irl_messaging"
    # topics drawn from recent interaction summaries
    assert any("threat detection" in t.lower() for t in ada["topics"])


def test_capsules_note_truncated(db):
    long = "x" * 400
    db.table("contacts").insert(
        {"full_name": "Verbose Vic", "notes": long,
         "connection_status": "contact_on_file", "closeness_tier": "none"}).execute()
    r = runner.invoke(app, ["capsules", "--json"])
    assert r.exit_code == 0, r.output
    vic = next(c for c in json.loads(r.output) if c["name"] == "Verbose Vic")
    assert len(vic["note"]) <= 145  # ~140 + ellipsis
    assert vic["note"] != long


def test_capsules_topics_limited_to_two(db):
    c = db.table("contacts").insert(
        {"full_name": "Chatty Cat", "connection_status": "contact_on_file",
         "closeness_tier": "none"}).execute().data[0]
    db.table("interactions").insert([
        {"contact_id": c["id"], "kind": "message", "channel": "dm",
         "occurred_at": "2026-01-01", "summary": "oldest", "logged_by": "rahul"},
        {"contact_id": c["id"], "kind": "message", "channel": "dm",
         "occurred_at": "2026-03-01", "summary": "middle", "logged_by": "rahul"},
        {"contact_id": c["id"], "kind": "message", "channel": "dm",
         "occurred_at": "2026-05-01", "summary": "newest", "logged_by": "rahul"},
    ]).execute()
    r = runner.invoke(app, ["capsules", "--json"])
    cat = next(c for c in json.loads(r.output) if c["name"] == "Chatty Cat")
    assert len(cat["topics"]) <= 2
    assert "newest" in cat["topics"]  # most recent included
    assert "oldest" not in cat["topics"]  # oldest dropped


def test_capsules_accepts_list_filters(db):
    _seed(db)
    r = runner.invoke(app, ["capsules", "--role", "founder", "--json"])
    assert r.exit_code == 0, r.output
    names = {c["name"] for c in json.loads(r.output)}
    assert names == {"Ada Founder"}


def test_capsules_empty_is_exit_zero_empty_json(db):
    _seed(db)
    r = runner.invoke(app, ["capsules", "--company-category", "nope", "--json"])
    assert r.exit_code == 0, r.output
    assert json.loads(r.output) == []


# ----- Task 15: crm find -----

def test_find_returns_intent_and_candidates_shape(db):
    _seed(db)
    r = runner.invoke(app, ["find", "cybersecurity founder", "--json"])
    assert r.exit_code == 0, r.output
    out = json.loads(r.output)
    assert out["intent"] == "cybersecurity founder"
    assert isinstance(out["candidates"], list)
    # candidates are capsules
    assert all("name" in c and "topics" in c for c in out["candidates"])


def test_find_keyword_overlap_on_capsule_text(db):
    _seed(db)
    # "cybersecurity" only matches Ada via company_category; intent has no flags
    r = runner.invoke(app, ["find", "someone in cybersecurity", "--json"])
    assert r.exit_code == 0, r.output
    names = {c["name"] for c in json.loads(r.output)["candidates"]}
    assert "Ada Founder" in names
    assert "Cleo Chief" not in names  # fintech, no keyword overlap


def test_find_keyword_matches_notes(db):
    db.table("contacts").insert(
        {"full_name": "Note Nan", "notes": "Deep expertise in quantum cryptography.",
         "connection_status": "contact_on_file", "closeness_tier": "none"}).execute()
    r = runner.invoke(app, ["find", "quantum cryptography expert", "--json"])
    assert r.exit_code == 0, r.output
    names = {c["name"] for c in json.loads(r.output)["candidates"]}
    assert "Note Nan" in names


def test_find_union_structural_and_keyword(db):
    by = _seed(db)
    # --role founder catches Ada; keyword "fintech" catches Cleo via company_category
    r = runner.invoke(app, ["find", "fintech leader", "--role", "founder", "--json"])
    assert r.exit_code == 0, r.output
    names = {c["name"] for c in json.loads(r.output)["candidates"]}
    assert {"Ada Founder", "Cleo Chief"} <= names


def test_find_keyword_matches_current_role(db):
    # role/company-based intents must surface people off current_role even before
    # company_category enrichment exists.
    db.table("contacts").insert(
        {"full_name": "Vera VC", "current_role": "Venture Partner",
         "current_company": "Acme Capital",
         "connection_status": "contact_on_file", "closeness_tier": "none"}).execute()
    r = runner.invoke(app, ["find", "venture partner", "--json"])
    assert r.exit_code == 0, r.output
    names = {c["name"] for c in json.loads(r.output)["candidates"]}
    assert "Vera VC" in names


def test_find_keyword_matches_current_company(db):
    db.table("contacts").insert(
        {"full_name": "Cory Capital", "current_role": "Principal",
         "current_company": "Sequoia Capital",
         "connection_status": "contact_on_file", "closeness_tier": "none"}).execute()
    r = runner.invoke(app, ["find", "sequoia investor", "--json"])
    assert r.exit_code == 0, r.output
    names = {c["name"] for c in json.loads(r.output)["candidates"]}
    assert "Cory Capital" in names


def test_find_no_match_empty_candidates(db):
    _seed(db)
    r = runner.invoke(app, ["find", "underwater basketweaving zzzzqqq", "--json"])
    assert r.exit_code == 0, r.output
    assert json.loads(r.output)["candidates"] == []
