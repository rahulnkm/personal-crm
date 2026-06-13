from typer.testing import CliRunner

from crm.cli import app

runner = CliRunner()


def _seed(db, name):
    return db.table("contacts").insert({"full_name": name}).execute().data[0]


def test_log_touchpoint_updates_denorm(db):
    c = _seed(db, "Ada Lovelace")
    r = runner.invoke(app, ["log", c["id"], "--kind", "call", "--channel", "telegram",
                            "--date", "2026-06-01", "--summary", "caught up re: NS"])
    assert r.exit_code == 0, r.output
    row = db.table("contacts").select("last_touchpoint_at,last_touchpoint_channel") \
        .eq("id", c["id"]).single().execute().data
    assert row["last_touchpoint_at"] == "2026-06-01"
    assert row["last_touchpoint_channel"] == "telegram"


def test_event_with_participants(db):
    a, b = _seed(db, "Ada"), _seed(db, "Grace")
    r = runner.invoke(app, ["event", "add", "NS dinner",
                            "--date", "2026-05-20", "--location", "Forest City",
                            "--participants", f"{a['id']},{b['id']}",
                            "--notes", "intro dinner, talked agents"])
    assert r.exit_code == 0, r.output
    events = db.table("events").select("*").execute().data
    assert len(events) == 1
    inter = db.table("interactions").select("contact_id,event_id,kind").execute().data
    assert len(inter) == 2                      # one per participant
    assert all(i["event_id"] == events[0]["id"] for i in inter)  # linked to SAME event


def test_event_per_person_note(db):
    a = _seed(db, "Ada")
    r = runner.invoke(app, ["event", "add", "Dinner", "--participants", a["id"]])
    assert r.exit_code == 0, r.output
    event_id = db.table("events").select("id").execute().data[0]["id"]  # from fixture, not output parsing
    r = runner.invoke(app, ["event", "note", event_id, a["id"],
                            "1:1 about her fundraise"])
    assert r.exit_code == 0
    inter = db.table("interactions").select("summary").eq("contact_id", a["id"]).execute().data
    assert any("fundraise" in (i["summary"] or "") for i in inter)


def test_log_invalid_kind_and_date_fail_cleanly(db):
    c = _seed(db, "Ada")
    r = runner.invoke(app, ["log", c["id"], "--kind", "chat"])
    assert r.exit_code == 1
    r = runner.invoke(app, ["log", c["id"], "--kind", "call", "--date", "06/01/2026"])
    assert r.exit_code == 1


def test_older_touchpoint_does_not_overwrite(db):
    c = _seed(db, "Ada")
    runner.invoke(app, ["log", c["id"], "--kind", "call", "--date", "2026-06-01"])
    runner.invoke(app, ["log", c["id"], "--kind", "email", "--date", "2026-01-15"])
    row = db.table("contacts").select("last_touchpoint_at").eq(
        "id", c["id"]).single().execute().data
    assert row["last_touchpoint_at"] == "2026-06-01"


def test_event_add_unresolvable_participant_writes_nothing(db):
    a = _seed(db, "Ada")
    r = runner.invoke(app, ["event", "add", "Ghost dinner",
                            "--participants", f"{a['id']},Nonexistent Person Xyz"])
    assert r.exit_code == 1
    assert db.table("events").select("id").execute().data == []        # no phantom event
    assert db.table("interactions").select("id").execute().data == []  # no partial writes
