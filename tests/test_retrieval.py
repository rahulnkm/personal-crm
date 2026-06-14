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
