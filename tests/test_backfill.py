# tests/test_backfill.py
from typer.testing import CliRunner

from crm.cli import app

runner = CliRunner()


def _contact_with_identity(db, name, **keys):
    c = db.table("contacts").insert({"full_name": name}).execute().data[0]
    db.table("contact_identities").insert(
        {"contact_id": c["id"], "source": "seed", **keys}).execute()
    return c


def _stage_tp(db, **fields):
    row = {"source": "tp_test", "source_external_id": fields.get("summary", "x"),
           "kind": "message", "channel": "imessage"}
    row.update(fields)
    return db.table("staging_interactions").insert(row).execute().data[0]


def test_backfill_links_by_email_and_upgrades_tier(db):
    c = _contact_with_identity(db, "Ada Lovelace", email="ada@example.com")
    _stage_tp(db, email="ada@example.com", occurred_at="2026-05-01",
              summary="caught up")
    r = runner.invoke(app, ["backfill"])
    assert r.exit_code == 0, r.output
    inter = db.table("interactions").select("*").eq("contact_id", c["id"]).execute().data
    assert len(inter) == 1 and inter[0]["channel"] == "imessage"
    fresh = db.table("contacts").select("*").eq("id", c["id"]).single().execute().data
    assert fresh["closeness_tier"] == "t1_irl_messaging"   # upgraded from none
    assert fresh["last_touchpoint_at"] == "2026-05-01"


def test_backfill_never_downgrades_tier(db):
    c = db.table("contacts").insert(
        {"full_name": "Ada", "closeness_tier": "t1_irl_messaging"}).execute().data[0]
    db.table("contact_identities").insert(
        {"contact_id": c["id"], "source": "seed", "email": "a@b.co"}).execute()
    _stage_tp(db, email="a@b.co", occurred_at="2026-05-01", channel="twitter",
              summary="tweeted")
    runner.invoke(app, ["backfill"])
    fresh = db.table("contacts").select("closeness_tier").eq("id", c["id"]).single().execute().data
    assert fresh["closeness_tier"] == "t1_irl_messaging"


def test_backfill_orphans_unmatched_and_retry_links_later(db):
    _stage_tp(db, email="ghost@nowhere.co", occurred_at="2026-05-01", summary="s1")
    runner.invoke(app, ["backfill"])
    rows = db.table("staging_interactions").select("match_status").execute().data
    assert rows[0]["match_status"] == "orphaned"
    # the person arrives later (e.g. Apple Contacts import) — retry links them
    _contact_with_identity(db, "Ghost Person", email="ghost@nowhere.co")
    runner.invoke(app, ["backfill", "--retry-orphans"])
    rows = db.table("staging_interactions").select("match_status").execute().data
    assert rows[0]["match_status"] == "linked"


def test_backfill_event_rows_share_one_event(db):
    _contact_with_identity(db, "Ada", email="a@b.co")
    _contact_with_identity(db, "Grace", email="g@b.co")
    for email, s in (("a@b.co", "s1"), ("g@b.co", "s2")):
        _stage_tp(db, email=email, kind="event", channel="irl",
                  occurred_at="2026-05-20", event_name="NS dinner", summary=s)
    runner.invoke(app, ["backfill"])
    events = db.table("events").select("*").execute().data
    assert len(events) == 1 and events[0]["name"] == "NS dinner"
    inter = db.table("interactions").select("event_id").execute().data
    assert all(i["event_id"] == events[0]["id"] for i in inter)


def test_backfill_rerun_is_idempotent(db):
    _contact_with_identity(db, "Ada", email="a@b.co")
    _stage_tp(db, email="a@b.co", occurred_at="2026-05-01", summary="once")
    runner.invoke(app, ["backfill"])
    # simulate crash-before-patch: force the staged row back to pending
    db.table("staging_interactions").update({"match_status": "pending"}).neq(
        "source", "___").execute()
    runner.invoke(app, ["backfill"])
    inter = db.table("interactions").select("id").execute().data
    assert len(inter) == 1   # select-first idempotency — no duplicate interaction


def test_backfill_refresh_updates_interaction_and_bump(db):
    _contact_with_identity(db, "Ada", email="a@b.co")
    row = _stage_tp(db, email="a@b.co", occurred_at="2026-05-01", summary="2 messages")
    runner.invoke(app, ["backfill"])
    # importer re-stages the same external id with newer data → re-pend + refresh
    db.table("staging_interactions").update(
        {"match_status": "pending", "occurred_at": "2026-06-01",
         "summary": "5 messages"}).eq("id", row["id"]).execute()
    runner.invoke(app, ["backfill"])
    inter = db.table("interactions").select("*").execute().data
    assert len(inter) == 1
    assert inter[0]["occurred_at"] == "2026-06-01"
    assert inter[0]["summary"] == "5 messages"
    contact = db.table("contacts").select("last_touchpoint_at").eq(
        "id", inter[0]["contact_id"]).single().execute().data
    assert contact["last_touchpoint_at"] == "2026-06-01"


def test_claim_is_exclusive_and_stale_claims_reset(db):
    _contact_with_identity(db, "Ada", email="a@b.co")
    _stage_tp(db, email="a@b.co", occurred_at="2026-05-01", summary="s1")
    # simulate a crashed worker: row stranded in 'claimed'
    db.table("staging_interactions").update({"match_status": "claimed"}).neq(
        "source", "___").execute()
    r = runner.invoke(app, ["backfill"])
    assert r.exit_code == 0, r.output
    rows = db.table("staging_interactions").select("match_status").execute().data
    assert rows[0]["match_status"] == "linked"   # reset → reclaimed → processed


def test_workers_one_behaves_identically(db):
    _contact_with_identity(db, "Ada", email="a@b.co")
    _stage_tp(db, email="a@b.co", occurred_at="2026-05-01", summary="solo")
    r = runner.invoke(app, ["backfill", "--workers", "1"])
    assert r.exit_code == 0, r.output
    inter = db.table("interactions").select("*").execute().data
    assert len(inter) == 1
    c = db.table("contacts").select("closeness_tier,last_touchpoint_at").eq(
        "id", inter[0]["contact_id"]).single().execute().data
    assert c["closeness_tier"] == "t1_irl_messaging"
    assert c["last_touchpoint_at"] == "2026-05-01"
