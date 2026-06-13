# tests/test_import_imessage.py
import sqlite3

from typer.testing import CliRunner

from crm.cli import app

runner = CliRunner()

APPLE_EPOCH_NS_2026_05_01 = 799_286_400 * 1_000_000_000  # 2026-05-01 vs 2001-01-01


def make_chatdb(tmp_path):
    p = tmp_path / "chat.db"
    con = sqlite3.connect(p)
    con.executescript(f"""
        create table handle (ROWID integer primary key, id text);
        create table message (ROWID integer primary key, handle_id integer,
                              date integer, is_from_me integer);
        insert into handle values (1, '+14155552671');
        insert into handle values (2, 'ada@example.com');
        insert into message values (1, 1, {APPLE_EPOCH_NS_2026_05_01}, 0);
        insert into message values (2, 1, {APPLE_EPOCH_NS_2026_05_01 - 86_400_000_000_000}, 1);
        insert into message values (3, 2, {APPLE_EPOCH_NS_2026_05_01}, 1);
    """)
    con.commit(); con.close()
    return p


def test_imessage_stages_touchpoints_per_handle(db, tmp_path):
    fixture = make_chatdb(tmp_path)
    r = runner.invoke(app, ["import", "imessage", "--db", str(fixture)])
    assert r.exit_code == 0, r.output
    rows = db.table("staging_interactions").select("*").eq("source", "imessage").execute().data
    assert len(rows) == 2
    phone_row = next(x for x in rows if x["phone"] == "+14155552671")
    assert phone_row["kind"] == "message" and phone_row["channel"] == "imessage"
    assert phone_row["occurred_at"] == "2026-05-01"
    assert "2 messages" in phone_row["summary"]


def test_imessage_end_to_end_links_and_upgrades(db, tmp_path):
    c = db.table("contacts").insert({"full_name": "Ada Lovelace"}).execute().data[0]
    db.table("contact_identities").insert(
        {"contact_id": c["id"], "source": "seed", "phone": "+14155552671"}).execute()
    runner.invoke(app, ["import", "imessage", "--db", str(make_chatdb(tmp_path))])
    r = runner.invoke(app, ["backfill"])
    assert r.exit_code == 0
    fresh = db.table("contacts").select("*").eq("id", c["id"]).single().execute().data
    assert fresh["closeness_tier"] == "t1_irl_messaging"
    assert fresh["last_touchpoint_at"] == "2026-05-01"


def test_imessage_missing_db_fails_with_fda_hint(db):
    r = runner.invoke(app, ["import", "imessage", "--db", "/tmp/nope-chat.db"])
    assert r.exit_code == 1


def test_imessage_reimport_refreshes_touchpoint(db, tmp_path):
    c = db.table("contacts").insert({"full_name": "Ada"}).execute().data[0]
    db.table("contact_identities").insert(
        {"contact_id": c["id"], "source": "seed", "phone": "+14155552671"}).execute()
    fixture = make_chatdb(tmp_path)
    runner.invoke(app, ["import", "imessage", "--db", str(fixture)])
    runner.invoke(app, ["backfill"])
    # newer message arrives
    con = sqlite3.connect(fixture)
    con.execute(f"insert into message values (9, 1, {APPLE_EPOCH_NS_2026_05_01 + 30*86_400_000_000_000}, 0)")
    con.commit(); con.close()
    runner.invoke(app, ["import", "imessage", "--db", str(fixture)])
    runner.invoke(app, ["backfill"])
    inter = db.table("interactions").select("*").eq("source", "imessage").execute().data
    assert len(inter) == 1                       # refreshed, not duplicated
    assert inter[0]["occurred_at"] == "2026-05-31"
    assert "3 messages" in inter[0]["summary"]
