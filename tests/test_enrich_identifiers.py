from typer.testing import CliRunner

from crm.cli import app

runner = CliRunner()


def _reg(db):
    db.table("agents").upsert({"id": "claude-web", "description": "test"}, on_conflict="id").execute()


def test_identifier_no_match_quarantines(db):
    _reg(db)
    c = db.table("contacts").insert({"full_name": "Ada"}).execute().data[0]
    r = runner.invoke(app, ["enrich", "apply", c["id"], "--agent", "claude-web", "--json"],
                      input='{"field":"email","value":"ada@new.com","kind":"identifier","confidence":0.9,"source":"gravatar"}')
    assert r.exit_code == 0, r.output
    ci = db.table("candidate_identities").select("*").eq("contact_id", c["id"]).execute().data
    assert len(ci) == 1
    assert ci[0]["status"] == "pending" and ci[0]["kind"] == "email"
    # not yet a live identity
    assert db.table("contact_identities").select("id").eq("contact_id", c["id"]).eq("email", "ada@new.com").execute().data == []


def test_identifier_matches_self_is_noop(db):
    _reg(db)
    c = db.table("contacts").insert({"full_name": "Ada"}).execute().data[0]
    db.table("contact_identities").insert(
        {"contact_id": c["id"], "source": "seed", "email": "ada@known.com"}).execute()
    r = runner.invoke(app, ["enrich", "apply", c["id"], "--agent", "claude-web", "--json"],
                      input='{"field":"email","value":"ada@known.com","kind":"identifier","confidence":0.9,"source":"gravatar"}')
    assert r.exit_code == 0, r.output
    assert "noop" in r.output
    assert db.table("candidate_identities").select("id").eq("contact_id", c["id"]).execute().data == []


def test_identifier_matches_other_contact_goes_to_review(db):
    _reg(db)
    a = db.table("contacts").insert({"full_name": "Ada"}).execute().data[0]
    b = db.table("contacts").insert({"full_name": "Bob"}).execute().data[0]
    db.table("contact_identities").insert(
        {"contact_id": b["id"], "source": "seed", "email": "shared@x.com"}).execute()
    r = runner.invoke(app, ["enrich", "apply", a["id"], "--agent", "claude-web", "--json"],
                      input='{"field":"email","value":"shared@x.com","kind":"identifier","confidence":0.9,"source":"gravatar"}')
    assert r.exit_code == 0, r.output
    rev = db.table("enrich_review").select("*").eq("contact_id", a["id"]).execute().data
    assert len(rev) == 1
    assert rev[0]["reason"] == "identifier_conflict"
    assert rev[0]["other_contact_id"] == b["id"]
    assert db.table("contact_identities").select("id").eq("contact_id", a["id"]).execute().data == []


def test_promotion_on_approve_creates_identity(db):
    _reg(db)
    c = db.table("contacts").insert({"full_name": "Ada"}).execute().data[0]
    runner.invoke(app, ["enrich", "apply", c["id"], "--agent", "claude-web"],
                  input='{"field":"email","value":"ada@new.com","kind":"identifier","confidence":0.9,"source":"gravatar"}')
    ci = db.table("candidate_identities").select("id").eq("contact_id", c["id"]).single().execute().data
    # approve the pending identifier
    r = runner.invoke(app, ["enrich", "review", "--approve-identity", ci["id"], "--agent", "claude-web"])
    assert r.exit_code == 0, r.output
    real = db.table("contact_identities").select("*").eq("contact_id", c["id"]).eq("email", "ada@new.com").execute().data
    assert len(real) == 1
    promoted = db.table("candidate_identities").select("status").eq("id", ci["id"]).single().execute().data
    assert promoted["status"] == "promoted"


def test_promoted_identity_dedups_later_import(db):
    _reg(db)
    c = db.table("contacts").insert({"full_name": "Ada"}).execute().data[0]
    runner.invoke(app, ["enrich", "apply", c["id"], "--agent", "claude-web"],
                  input='{"field":"email","value":"ada@new.com","kind":"identifier","confidence":0.9,"source":"gravatar"}')
    ci = db.table("candidate_identities").select("id").eq("contact_id", c["id"]).single().execute().data
    runner.invoke(app, ["enrich", "review", "--approve-identity", ci["id"], "--agent", "claude-web"])
    # find_candidates with the same email should now resolve to this contact
    from crm.matching import find_candidates
    from crm.config import get_client
    hit = find_candidates(get_client(), {"email": "ada@new.com"})
    assert hit is not None and hit["contact_id"] == c["id"]
