from typer.testing import CliRunner

from crm.cli import app

runner = CliRunner()


def _seed_review_pair(db):
    db.table("staging").insert({"source": "s1", "source_external_id": "a",
                                "full_name": "Jonathan Smithers"}).execute()
    runner.invoke(app, ["dedup"])
    db.table("staging").insert({"source": "s2", "source_external_id": "b",
                                "full_name": "Jonathon Smithers"}).execute()
    runner.invoke(app, ["dedup"])
    row = (db.table("staging").select("*")
           .eq("match_status", "needs_review").execute().data)
    assert row, "expected a needs_review row"
    return row[0]


def test_review_list_shows_pair(db):
    _seed_review_pair(db)
    r = runner.invoke(app, ["review", "--json"])
    assert r.exit_code == 0
    assert "Jonathon Smithers" in r.output


def test_review_approve_attaches(db):
    row = _seed_review_pair(db)
    r = runner.invoke(app, ["review", "--approve", row["id"]])
    assert r.exit_code == 0
    assert len(db.table("contacts").select("id").execute().data) == 1
    assert len(db.table("contact_identities").select("id").execute().data) == 2


def test_review_reject_creates_new_contact(db):
    row = _seed_review_pair(db)
    runner.invoke(app, ["review", "--reject", row["id"]])
    assert len(db.table("contacts").select("id").execute().data) == 2


def test_merge_self_is_rejected(db):
    a = db.table("contacts").insert({"full_name": "Ada"}).execute().data[0]
    r = runner.invoke(app, ["merge", a["id"], a["id"]])
    assert r.exit_code == 2
    assert len(db.table("contacts").select("id").execute().data) == 1  # still alive


def test_merge_preserves_tier_tags_notes(db):
    keep = db.table("contacts").insert({"full_name": "Ada L"}).execute().data[0]
    drop = db.table("contacts").insert(
        {"full_name": "Ada Lovelace", "closeness_tier": "t1_irl_messaging",
         "connection_status": "in_network", "tags": ["mentor"],
         "notes": "met at NS"}).execute().data[0]
    runner.invoke(app, ["tags", "add", "mentor", "--desc", "test"])  # registry hygiene
    r = runner.invoke(app, ["merge", keep["id"], drop["id"]])
    assert r.exit_code == 0
    k = db.table("contacts").select("*").eq("id", keep["id"]).single().execute().data
    assert k["closeness_tier"] == "t1_irl_messaging"
    assert k["connection_status"] == "in_network"
    assert "mentor" in k["tags"]
    assert "met at NS" in (k["notes"] or "")


def test_approve_with_deleted_candidate_fails_cleanly(db):
    row = _seed_review_pair(db)
    # simulate: the candidate contact got merged away → FK set matched_contact_id null
    db.table("staging").update({"matched_contact_id": None}).eq("id", row["id"]).execute()
    r = runner.invoke(app, ["review", "--approve", row["id"]])
    assert r.exit_code == 1


def test_merge_and_split(db):
    a = db.table("contacts").insert({"full_name": "Ada L"}).execute().data[0]
    b = db.table("contacts").insert({"full_name": "Ada Lovelace"}).execute().data[0]
    ident_b = db.table("contact_identities").insert(
        {"contact_id": b["id"], "source": "s", "email": "x@y.z"}
    ).execute().data[0]
    r = runner.invoke(app, ["merge", a["id"], b["id"]])
    assert r.exit_code == 0
    assert len(db.table("contacts").select("id").execute().data) == 1
    moved = db.table("contact_identities").select("contact_id").execute().data
    assert moved[0]["contact_id"] == a["id"]
    r = runner.invoke(app, ["split", a["id"], ident_b["id"]])
    assert r.exit_code == 0
    assert len(db.table("contacts").select("id").execute().data) == 2
