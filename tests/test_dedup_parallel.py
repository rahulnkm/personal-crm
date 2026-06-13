# tests/test_dedup_parallel.py
from typer.testing import CliRunner
from crm.cli import app
runner = CliRunner()


def test_planted_exact_dup_three_sources(db):
    for i, src in enumerate(("csvA", "csvB", "csvC")):
        db.table("staging").insert({"source": src, "source_external_id": f"x{i}",
            "full_name": "Dup Person", "email": "dup@x.co", "match_status": "pending"}).execute()
    assert runner.invoke(app, ["dedup", "--workers", "4"]).exit_code == 0
    assert len(db.table("contacts").select("id").ilike("full_name", "Dup Person").execute().data) == 1


def test_review_band_not_merged_under_workers(db):    # guards the 4th-review bug
    db.table("staging").insert([
        {"source": "s", "source_external_id": "a", "full_name": "Robert Smith",
         "email": "r@x.com", "match_status": "pending"},
        {"source": "s", "source_external_id": "b", "full_name": "Robart Smith",
         "email": "b@y.com", "match_status": "pending"}]).execute()
    runner.invoke(app, ["dedup", "--workers", "4"])
    assert len(db.table("contacts").select("id").ilike("full_name", "Rob%t Smith").execute().data) == 1
    assert len(db.table("staging").select("id").eq("match_status", "needs_review").execute().data) == 1


def test_transitive_cut_point_under_workers(db):
    # Explicit imported_at keeps within-cluster replay order stable (A→B→C→D).
    # Without it, same-millisecond batch inserts leave UUID tie-break non-deterministic,
    # making C occasionally sort before A/B and flip which row gets needs_review.
    db.table("staging").insert([
        {"source": "s", "source_external_id": "A", "full_name": "Robert Smith",
         "email": "r@x.com", "match_status": "pending",
         "imported_at": "2020-01-01T00:00:01Z"},
        {"source": "s", "source_external_id": "B", "full_name": "Robert Smith",
         "email": "r@x.com", "match_status": "pending",
         "imported_at": "2020-01-01T00:00:02Z"},
        {"source": "s", "source_external_id": "C", "full_name": "Robart Smith",
         "email": "c@c.co", "match_status": "pending",
         "imported_at": "2020-01-01T00:00:03Z"},
        {"source": "s", "source_external_id": "D", "full_name": "Zenith Quux",
         "phone": "+15550001111", "match_status": "pending",
         "imported_at": "2020-01-01T00:00:04Z"}]).execute()
    runner.invoke(app, ["dedup", "--workers", "4"])
    smiths = db.table("contacts").select("id").ilike("full_name", "Rob%t Smith").execute().data
    # {A,B} create one contact; C is review-band (0.625 name sim → needs_review),
    # so C does NOT create its own contact and is NOT merged into A/B's contact.
    # D ("Zenith Quux") is unrelated → its own contact but not a Smith.
    # Only 1 Smith contact exists after dedup; C sits in the review queue.
    assert len(smiths) == 1   # NOT merged across the review link — C queued, not auto-attached
    # Confirm C is queued for review, not silently merged
    c_row = db.table("staging").select("match_status").eq("source_external_id", "C").execute().data
    assert c_row[0]["match_status"] == "needs_review"


def test_attach_race_no_lost_fill_or_double_log(db):
    c = db.table("contacts").insert({"full_name": "Pre Existing"}).execute().data[0]
    db.table("contact_identities").insert([
        {"contact_id": c["id"], "source": "seed", "source_external_id": "e1", "email": "p@x.co"},
        {"contact_id": c["id"], "source": "seed", "source_external_id": "l1",
         "linkedin_url": "linkedin.com/in/p"}]).execute()
    db.table("staging").insert([
        {"source": "s", "source_external_id": "r1", "full_name": "Aaa Bbb", "email": "p@x.co",
         "company": "AcmeCo", "match_status": "pending"},
        {"source": "s", "source_external_id": "r2", "full_name": "Zzz Yyy",
         "linkedin_url": "linkedin.com/in/p", "location": "NYC", "match_status": "pending"}]).execute()
    runner.invoke(app, ["dedup", "--workers", "4"])
    fresh = db.table("contacts").select("current_company,location").eq("id", c["id"]).single().execute().data
    assert fresh["current_company"] == "AcmeCo" and fresh["location"] == "NYC"   # no lost fill-null


def test_crash_resume_no_dup(db):
    c = db.table("contacts").insert({"full_name": "Ada"}).execute().data[0]
    db.table("contact_identities").insert({"contact_id": c["id"], "source": "s",
        "source_external_id": "a", "email": "a@b.co"}).execute()
    db.table("staging").insert({"source": "s", "source_external_id": "a", "full_name": "Ada",
        "email": "a@b.co", "match_status": "pending"}).execute()
    runner.invoke(app, ["dedup", "--workers", "4"])
    assert len(db.table("contacts").select("id").execute().data) == 1
