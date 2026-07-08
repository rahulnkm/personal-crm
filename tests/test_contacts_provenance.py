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
