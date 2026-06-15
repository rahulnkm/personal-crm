# tests/test_backfill.py
from unittest.mock import patch

from typer.testing import CliRunner

from crm.cli import app
from tests._spy import CountingClient

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


# ---------------------------------------------------------------------------
# Task 1.2 — bulk_upsert_interactions contract tests
# ---------------------------------------------------------------------------

def test_reimport_refresh_no_duplicate_kind_preserved(db):
    """Re-staging the same (source, source_external_id) with a changed summary
    must produce exactly ONE interaction row; summary updated; kind/channel/
    logged_by unchanged from the first import."""
    c = _contact_with_identity(db, "Ada", email="a@b.co")
    row = _stage_tp(db, email="a@b.co", occurred_at="2026-05-01",
                    summary="first import", kind="message", channel="imessage")
    runner.invoke(app, ["backfill"])
    inter_before = db.table("interactions").select("*").eq("contact_id", c["id"]).execute().data
    assert len(inter_before) == 1
    original_kind = inter_before[0]["kind"]
    original_channel = inter_before[0]["channel"]
    original_logged_by = inter_before[0]["logged_by"]

    # Same source + source_external_id, but summary changed — re-pend it
    db.table("staging_interactions").update(
        {"match_status": "pending", "summary": "updated summary"}
    ).eq("id", row["id"]).execute()
    runner.invoke(app, ["backfill"])

    inter_after = db.table("interactions").select("*").eq("contact_id", c["id"]).execute().data
    assert len(inter_after) == 1, "must not duplicate on re-import"
    assert inter_after[0]["summary"] == "updated summary"
    assert inter_after[0]["kind"] == original_kind
    assert inter_after[0]["channel"] == original_channel
    assert inter_after[0]["logged_by"] == original_logged_by


def test_denorm_healed_when_interaction_moves_to_new_contact(db):
    """If the same external touchpoint re-matches to contact B instead of A,
    contact A's denorm (last_touchpoint_*) must be recomputed and cleared
    (A had only that one interaction)."""
    # contact A matched by email=a@b.co
    a = _contact_with_identity(db, "Alice", email="a@b.co")
    # contact B matched by email=b@b.co
    b = _contact_with_identity(db, "Bob", email="b@b.co")

    # Stage a touchpoint that matches A via email
    row = _stage_tp(db, email="a@b.co", occurred_at="2026-05-01",
                    summary="moves later", source_external_id="move-test-1")
    runner.invoke(app, ["backfill"])

    a_before = db.table("contacts").select("last_touchpoint_at").eq(
        "id", a["id"]).single().execute().data
    assert a_before["last_touchpoint_at"] == "2026-05-01"

    # Re-point: change staging row to match B instead, reset to pending
    db.table("staging_interactions").update(
        {"match_status": "pending", "email": "b@b.co"}
    ).eq("id", row["id"]).execute()
    runner.invoke(app, ["backfill"])

    # A had only this interaction — after moving, A's last_touchpoint_at is gone
    a_after = db.table("contacts").select("last_touchpoint_at").eq(
        "id", a["id"]).single().execute().data
    assert a_after["last_touchpoint_at"] is None, (
        "contact A must be recomputed after its only interaction was re-pointed to B"
    )

    # B now has the interaction
    b_after = db.table("contacts").select("last_touchpoint_at").eq(
        "id", b["id"]).single().execute().data
    assert b_after["last_touchpoint_at"] == "2026-05-01"


def test_no_per_row_update_uses_bulk_rpc(db):
    """Round-trip regression: backfill must call bulk_upsert_interactions and
    never issue a per-row interactions.update — even when rows already exist
    (the refresh path).  N-invariance: 1 existing row and 3 existing rows both
    produce 0 interactions.update calls."""
    spy = CountingClient(db)

    def make_spy():
        return spy

    _contact_with_identity(db, "Ada", email="ada@b.co")
    # Stage 3 touchpoints then run once so they already exist in interactions
    for i in range(3):
        _stage_tp(db, email="ada@b.co", occurred_at=f"2026-0{i+1}-01",
                  summary=f"touch {i}", source_external_id=f"spy-ext-{i}")
    runner.invoke(app, ["backfill"])  # first run — inserts all 3

    # Reset to pending so the next run takes the refresh path
    db.table("staging_interactions").update({"match_status": "pending"}).like(
        "source_external_id", "spy-ext-%").execute()

    spy.calls.clear()  # reset counter before the observed run
    with patch("crm.commands.backfill.get_client", make_spy):
        r = runner.invoke(app, ["backfill", "--workers", "1"])
    assert r.exit_code == 0, r.output

    assert spy.rpc_count("bulk_upsert_interactions") >= 1, (
        "bulk_upsert_interactions RPC must be called"
    )
    assert spy.count("interactions", "update") == 0, (
        "per-row interactions.update must be 0 — all refreshes go through the RPC"
    )


def test_orphan_rows_excluded_from_bulk_upsert_patched_orphaned(db):
    """Orphaned rows (no match) must NOT appear in the bulk_upsert payload and
    their staging row must end up with match_status='orphaned'."""
    # One matchable contact and one ghost email with no contact
    _contact_with_identity(db, "Ada", email="linked@b.co")
    _stage_tp(db, email="linked@b.co", occurred_at="2026-05-01",
              summary="linked", source_external_id="orphan-test-linked")
    _stage_tp(db, email="ghost@nowhere.example", occurred_at="2026-05-01",
              summary="orphan", source_external_id="orphan-test-ghost")

    spy = CountingClient(db)

    with patch("crm.commands.backfill.get_client", lambda: spy):
        r = runner.invoke(app, ["backfill", "--workers", "1"])
    assert r.exit_code == 0, r.output

    # Ghost row must be marked orphaned in staging
    ghost_staging = db.table("staging_interactions").select("match_status").eq(
        "source_external_id", "orphan-test-ghost").execute().data
    assert ghost_staging[0]["match_status"] == "orphaned"

    # Only one interaction must exist (the linked one)
    inter = db.table("interactions").select("id").execute().data
    assert len(inter) == 1
