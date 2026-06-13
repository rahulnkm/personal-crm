from typer.testing import CliRunner

from crm.cli import app

runner = CliRunner()


def _stage(db, source="s1", **fields):
    row = {"source": source,
           "source_external_id": fields.get("full_name", "x") + source}
    row.update(fields)
    return db.table("staging").insert(row).execute().data[0]


def test_dedup_creates_new_contact(db):
    _stage(db, full_name="Ada Lovelace", email="ada@example.com", company="Analytical")
    r = runner.invoke(app, ["dedup"])
    assert r.exit_code == 0, r.output
    contacts = db.table("contacts").select("*").execute().data
    assert len(contacts) == 1
    assert contacts[0]["full_name"] == "Ada Lovelace"
    assert contacts[0]["current_company"] == "Analytical"
    idents = db.table("contact_identities").select("*").execute().data
    assert len(idents) == 1 and idents[0]["email"] == "ada@example.com"


def test_dedup_attaches_exact_match_and_fills_nulls(db):
    _stage(db, source="s1", full_name="Ada Lovelace", email="ada@example.com")
    runner.invoke(app, ["dedup"])
    # second source, same email, brings a company; name conflicts
    _stage(db, source="s2", full_name="Ada K. Lovelace", email="ada@example.com",
           company="Analytical Engines")
    runner.invoke(app, ["dedup"])
    contacts = db.table("contacts").select("*").execute().data
    assert len(contacts) == 1                                # attached, not duplicated
    assert contacts[0]["current_company"] == "Analytical Engines"  # null filled
    assert contacts[0]["full_name"] == "Ada Lovelace"        # existing value survives
    assert len(db.table("contact_identities").select("id").execute().data) == 2
    # conflict was logged, not silently dropped
    log = db.table("enrichment_log").select("*").eq("field", "full_name").execute().data
    assert log and log[0]["new_value"] == "Ada K. Lovelace"


def test_dedup_queues_ambiguous_for_review(db):
    _stage(db, source="s1", full_name="Jonathan Smithers")
    runner.invoke(app, ["dedup"])
    _stage(db, source="s2", full_name="Jonathon Smithers")  # fuzzy, sub-auto score
    runner.invoke(app, ["dedup"])
    staged = (db.table("staging").select("match_status,match_confidence")
              .eq("source", "s2").execute().data)
    assert staged[0]["match_status"] == "needs_review"
    assert db.table("contacts").select("id").execute().data.__len__() == 1


def test_rerun_after_partial_attach_crash_recovers(db):
    # simulate: identity inserted, staging row never patched (crash mid-_attach)
    c = db.table("contacts").insert({"full_name": "Ada Lovelace"}).execute().data[0]
    db.table("contact_identities").insert(
        {"contact_id": c["id"], "source": "s1", "source_external_id": "rowhash1",
         "email": "ada@example.com"}
    ).execute()
    db.table("staging").insert(
        {"source": "s1", "source_external_id": "rowhash1",
         "full_name": "Ada Lovelace", "email": "ada@example.com",
         "match_status": "pending"}
    ).execute()
    r = runner.invoke(app, ["dedup"])
    assert r.exit_code == 0, r.output          # does NOT crash on 23505
    assert len(db.table("contact_identities").select("id").execute().data) == 1  # no dupe
    staged = db.table("staging").select("match_status").eq(
        "source_external_id", "rowhash1").execute().data
    assert staged[0]["match_status"] == "auto_matched"   # row resolved on rerun


def test_dedup_workers_one_equivalent(db):
    db.table("staging").insert([
        {"source": "s", "source_external_id": "a", "full_name": "Ada", "email": "a@b.co",
         "match_status": "pending"},
        {"source": "s", "source_external_id": "b", "full_name": "Ada", "email": "a@b.co",
         "match_status": "pending"},
    ]).execute()
    r = runner.invoke(app, ["dedup", "--workers", "1"])
    assert r.exit_code == 0, r.output
    assert len(db.table("contacts").select("id").execute().data) == 1
    assert len(db.table("contact_identities").select("id").execute().data) == 2


def test_dedup_resume_after_partial_create(db):
    # a prior run already created the contact+anchor identity; staging still pending
    c = db.table("contacts").insert({"full_name": "Ada"}).execute().data[0]
    db.table("contact_identities").insert(
        {"contact_id": c["id"], "source": "s", "source_external_id": "a",
         "email": "a@b.co"}).execute()
    db.table("staging").insert(
        {"source": "s", "source_external_id": "a", "full_name": "Ada", "email": "a@b.co",
         "match_status": "pending"}).execute()
    r = runner.invoke(app, ["dedup"])
    assert r.exit_code == 0, r.output
    assert len(db.table("contacts").select("id").execute().data) == 1   # no dup
    assert len(db.table("contact_identities").select("id").execute().data) == 1
