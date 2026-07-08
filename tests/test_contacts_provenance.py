from typer.testing import CliRunner

from crm.cli import app

runner = CliRunner()


def test_set_scalar_writes_manual_provenance(db):
    c = db.table("contacts").insert({"full_name": "Ada"}).execute().data[0]
    r = runner.invoke(app, ["set", c["id"], "current_company=Analytical"])
    assert r.exit_code == 0, r.output
    got = db.table("contacts").select("current_company").eq("id", c["id"]).single().execute().data
    assert got["current_company"] == "Analytical"
    rows = (db.table("enrichment_log").select("method,is_current,new_value")
            .eq("contact_id", c["id"]).eq("field", "current_company").execute().data)
    cur = [row for row in rows if row["is_current"]]
    assert len(cur) == 1
    assert cur[0]["method"] == "manual_set"
    assert cur[0]["new_value"] == "Analytical"


def test_set_manual_blocks_subsequent_enrich(db):
    c = db.table("contacts").insert({"full_name": "Bob"}).execute().data[0]
    runner.invoke(app, ["set", c["id"], "current_company=RealCo"])
    out = db.rpc("enrich_apply_candidate", {
        "p_contact_id": c["id"], "p_field": "current_company", "p_value": "BrokerCo",
        "p_method": "enrich_api", "p_source": "pdl", "p_confidence": 0.95,
        "p_source_detail": None, "p_dry_run": False}).execute().data
    assert out in ("review", "losing")
    got = db.table("contacts").select("current_company").eq("id", c["id"]).single().execute().data
    assert got["current_company"] == "RealCo"


def test_set_blank_is_deliberate_null(db):
    c = db.table("contacts").insert({"full_name": "Cy", "location": "SF"}).execute().data[0]
    # seed provenance for the existing value so we have an is_current row
    db.rpc("enrich_seed_provenance", {}).execute()
    r = runner.invoke(app, ["set", c["id"], "location="])
    assert r.exit_code == 0, r.output
    got = db.table("contacts").select("location").eq("id", c["id"]).single().execute().data
    assert got["location"] is None


def test_set_array_keeps_union_path(db):
    c = db.table("contacts").insert({"full_name": "Di", "affiliations": ["a"]}).execute().data[0]
    r = runner.invoke(app, ["set", c["id"], "affiliations=b"])
    assert r.exit_code == 0, r.output
    got = db.table("contacts").select("affiliations").eq("id", c["id"]).single().execute().data
    assert set(got["affiliations"]) == {"a", "b"}


def test_contact_json_includes_provenance(db):
    c = db.table("contacts").insert({"full_name": "Eve"}).execute().data[0]
    db.rpc("enrich_apply_candidate", {
        "p_contact_id": c["id"], "p_field": "current_company", "p_value": "Gravatar Inc",
        "p_method": "enrich_api", "p_source": "gravatar", "p_confidence": 0.9,
        "p_source_detail": None, "p_dry_run": False}).execute()
    r = runner.invoke(app, ["contact", c["id"], "--json"])
    assert r.exit_code == 0, r.output
    import json
    out = json.loads(r.output)
    assert "provenance" in out
    p = out["provenance"]["current_company"]
    assert p["value"] == "Gravatar Inc"
    assert p["source"] == "gravatar"
    assert p["confidence"] == 0.9
    assert "as_of" in p
    assert "stale" in p


def test_contact_json_graceful_without_provenance(db):
    c = db.table("contacts").insert({"full_name": "Frank"}).execute().data[0]
    r = runner.invoke(app, ["contact", c["id"], "--json"])
    assert r.exit_code == 0, r.output
    import json
    out = json.loads(r.output)
    assert out["provenance"] == {}  # no is_current rows → empty map, no error


def test_note_writes_manual_provenance(db):
    from datetime import date
    c = db.table("contacts").insert({"full_name": "Jill"}).execute().data[0]
    r = runner.invoke(app, ["note", c["id"], "met at the summit"])
    assert r.exit_code == 0, r.output
    today = date.today().isoformat()
    first_blob = f"[{today} rahul] met at the summit"
    got = db.table("contacts").select("notes").eq("id", c["id"]).single().execute().data
    assert got["notes"] == first_blob
    rows = (db.table("enrichment_log")
            .select("method,source,new_value,source_detail,is_current")
            .eq("contact_id", c["id"]).eq("field", "notes").execute().data)
    cur = [row for row in rows if row["is_current"]]
    assert len(cur) == 1
    assert cur[0]["method"] == "manual_set"
    assert cur[0]["source"] == "rahul"
    assert cur[0]["new_value"] == first_blob
    assert cur[0]["source_detail"] == "met at the summit"

    r2 = runner.invoke(app, ["note", c["id"], "followed up over email"])
    assert r2.exit_code == 0, r2.output
    second_blob = f"{first_blob}\n[{today} rahul] followed up over email"
    got = db.table("contacts").select("notes").eq("id", c["id"]).single().execute().data
    assert got["notes"] == second_blob
    rows = (db.table("enrichment_log")
            .select("new_value,source_detail,is_current")
            .eq("contact_id", c["id"]).eq("field", "notes").execute().data)
    cur = [row for row in rows if row["is_current"]]
    assert len(cur) == 1
    assert cur[0]["new_value"] == second_blob
    assert cur[0]["source_detail"] == "followed up over email"
    stale = [row for row in rows if not row["is_current"]]
    assert [row["new_value"] for row in stale] == [first_blob]


def test_note_wins_after_review_approve(db):
    from datetime import date
    c = db.table("contacts").insert({"full_name": "Kai"}).execute().data[0]
    out = db.rpc("enrich_apply_candidate", {
        "p_contact_id": c["id"], "p_field": "notes", "p_value": "Joined DeepCo as CTO",
        "p_method": "enrich_api", "p_source": "pdl", "p_confidence": 0.5,
        "p_source_detail": None, "p_dry_run": False}).execute().data
    assert out == "review"
    item = (db.table("enrich_review").select("id").eq("contact_id", c["id"])
            .eq("field", "notes").eq("status", "open").execute().data)[0]
    r = runner.invoke(app, ["enrich", "review", "--approve", item["id"]])
    assert r.exit_code == 0, r.output
    # a manual_set is_current row now sits on notes — the note must still win
    r2 = runner.invoke(app, ["note", c["id"], "spoke at the offsite"])
    assert r2.exit_code == 0, r2.output
    today = date.today().isoformat()
    expected = f"Joined DeepCo as CTO\n[{today} rahul] spoke at the offsite"
    got = db.table("contacts").select("notes").eq("id", c["id"]).single().execute().data
    assert got["notes"] == expected
    cur = (db.table("enrichment_log").select("new_value,method")
           .eq("contact_id", c["id"]).eq("field", "notes")
           .eq("is_current", True).execute().data)
    assert len(cur) == 1
    assert cur[0]["new_value"] == expected
    assert cur[0]["method"] == "manual_set"


def test_note_manual_guard_routes_later_enrich_to_review(db):
    c = db.table("contacts").insert({"full_name": "Lena"}).execute().data[0]
    r = runner.invoke(app, ["note", c["id"], "intro'd by Sam at the AI dinner"])
    assert r.exit_code == 0, r.output
    blob = db.table("contacts").select("notes").eq("id", c["id"]).single().execute().data["notes"]
    out = db.rpc("enrich_apply_candidate", {
        "p_contact_id": c["id"], "p_field": "notes",
        "p_value": "Now leads platform engineering at OrbitalWorks",
        "p_method": "enrich_api", "p_source": "pdl", "p_confidence": 0.95,
        "p_source_detail": '"leads platform engineering at OrbitalWorks since May 2026"',
        "p_dry_run": False}).execute().data
    assert out == "review"
    got = db.table("contacts").select("notes").eq("id", c["id"]).single().execute().data
    assert got["notes"] == blob


# ----- birth provenance: newly created contacts log their birth field values -----

def test_dedup_bulk_create_writes_birth_provenance(db):
    # no match anywhere -> bulk create path (create_contacts_with_identities RPC)
    db.table("staging").insert(
        {"source": "s1", "source_external_id": "x1", "full_name": "Ada Lovelace",
         "role": "Mathematician", "company": "Analytical", "location": "London",
         "twitter_username": "adalovelace", "github_username": "ada-lovelace",
         "website_url": "https://ada.example.com", "match_status": "pending"}
    ).execute()
    r = runner.invoke(app, ["dedup"])
    assert r.exit_code == 0, r.output
    cid = db.table("contacts").select("id").execute().data[0]["id"]
    rows = (db.table("enrichment_log").select("*")
            .eq("contact_id", cid).eq("method", "import_create").execute().data)
    by_field = {row["field"]: row for row in rows}
    assert set(by_field) == {"full_name", "current_role", "current_company",
                             "location", "twitter_username", "github_username",
                             "website_url"}
    assert by_field["full_name"]["new_value"] == "Ada Lovelace"
    assert by_field["twitter_username"]["new_value"] == "adalovelace"  # W2 social rides along
    assert by_field["website_url"]["new_value"] == "https://ada.example.com"
    for row in rows:
        assert row["source"] == "s1"
        assert row["source_detail"] == "staging s1/x1"
        assert row["is_current"] is True


def test_dedup_bulk_create_skips_null_birth_fields(db):
    db.table("staging").insert(
        {"source": "s1", "source_external_id": "x2", "full_name": "Grace Hopper",
         "company": "Navy", "match_status": "pending"}).execute()
    r = runner.invoke(app, ["dedup"])
    assert r.exit_code == 0, r.output
    rows = (db.table("enrichment_log").select("field")
            .eq("method", "import_create").execute().data)
    assert {row["field"] for row in rows} == {"full_name", "current_company"}


def test_review_reject_create_writes_birth_provenance(db):
    from crm.commands.dedup import _create
    staged = db.table("staging").insert(
        {"source": "s3", "source_external_id": "h3", "full_name": "Ada Lovelace",
         "role": "Countess", "twitter_username": "adalovelace"}).execute().data[0]
    cid = _create(db, staged)
    rows = (db.table("enrichment_log").select("*")
            .eq("contact_id", cid).eq("method", "import_create").execute().data)
    by_field = {row["field"]: row for row in rows}
    assert set(by_field) == {"full_name", "current_role", "twitter_username"}
    assert by_field["current_role"]["new_value"] == "Countess"
    for row in rows:
        assert row["source"] == "s3"
        assert row["source_detail"] == "staging s3/h3"
        assert row["is_current"] is True


def test_add_writes_manual_add_provenance(db):
    r = runner.invoke(app, ["add", "Zed Zebra", "--role", "Engineer",
                            "--company", "ZCo", "--origin", "met at the summit"])
    assert r.exit_code == 0, r.output
    cid = r.stdout.strip().splitlines()[-1]
    rows = (db.table("enrichment_log").select("*")
            .eq("contact_id", cid).eq("method", "manual_add").execute().data)
    by_field = {row["field"]: row for row in rows}
    assert set(by_field) == {"full_name", "current_role", "current_company",
                             "origin_context"}
    assert by_field["full_name"]["new_value"] == "Zed Zebra"
    assert by_field["origin_context"]["new_value"] == "met at the summit"
    for row in rows:
        assert row["source"] == "rahul"
        assert row["source_detail"] is None   # manual ground truth: no span
        assert row["is_current"] is True


def test_add_skips_null_birth_fields(db):
    r = runner.invoke(app, ["add", "Solo Name"])
    assert r.exit_code == 0, r.output
    cid = r.stdout.strip().splitlines()[-1]
    rows = (db.table("enrichment_log").select("field")
            .eq("contact_id", cid).eq("method", "manual_add").execute().data)
    assert [row["field"] for row in rows] == ["full_name"]


# ----- Task 16: full dossier bundle -----

def test_contact_dossier_bundle(db):
    import json
    c = db.table("contacts").insert(
        {"full_name": "Grace", "origin_context": "Met at a hackathon in 2024",
         "last_touchpoint_at": "2026-04-10", "last_touchpoint_channel": "email",
         "last_touchpoint_topic": "intro to her cofounder"}).execute().data[0]
    db.table("interactions").insert([
        {"contact_id": c["id"], "kind": "message", "channel": "dm",
         "occurred_at": "2026-01-15", "summary": "first ping", "logged_by": "rahul"},
        {"contact_id": c["id"], "kind": "email", "channel": "email",
         "occurred_at": "2026-04-10", "summary": "intro to her cofounder",
         "logged_by": "rahul"},
    ]).execute()
    r = runner.invoke(app, ["contact", c["id"], "--json"])
    assert r.exit_code == 0, r.output
    out = json.loads(r.output)
    # origin_context surfaced explicitly
    assert out["origin_context"] == "Met at a hackathon in 2024"
    # interactions ordered desc, with the required fields
    inter = out["interactions"]
    assert [i["occurred_at"] for i in inter] == ["2026-04-10", "2026-01-15"]
    assert all({"occurred_at", "channel", "summary"} <= set(i) for i in inter)
    # last_touchpoint denormalized block
    lt = out["last_touchpoint"]
    assert lt["at"] == "2026-04-10"
    assert lt["channel"] == "email"
    assert lt["topic"] == "intro to her cofounder"


def test_contact_dossier_interactions_limited(db):
    import json
    c = db.table("contacts").insert({"full_name": "Hank"}).execute().data[0]
    rows = [{"contact_id": c["id"], "kind": "message", "channel": "dm",
             "occurred_at": f"2026-{m:02d}-01", "summary": f"msg {m}",
             "logged_by": "rahul"} for m in range(1, 13)] * 3  # 36 interactions
    db.table("interactions").insert(rows).execute()
    r = runner.invoke(app, ["contact", c["id"], "--json"])
    assert r.exit_code == 0, r.output
    out = json.loads(r.output)
    assert len(out["interactions"]) <= 20


def test_contact_dossier_no_touchpoint_graceful(db):
    import json
    c = db.table("contacts").insert({"full_name": "Iris"}).execute().data[0]
    r = runner.invoke(app, ["contact", c["id"], "--json"])
    assert r.exit_code == 0, r.output
    out = json.loads(r.output)
    assert out["interactions"] == []
    assert out["last_touchpoint"]["at"] is None
    assert "origin_context" in out
