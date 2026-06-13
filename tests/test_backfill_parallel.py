# tests/test_backfill_parallel.py
"""Parallel-specific guarantees: no duplicates/losses under 4 workers, and the
declared behavioral broadening (manually-logged channels now count as tier
evidence via recompute)."""
from typer.testing import CliRunner

from crm.cli import app

runner = CliRunner()


def test_parallel_smoke_no_dupes_no_losses(db):
    # 150 contacts, 2 touchpoints each = 300 staged rows across 3+ pages
    contacts = [{"full_name": f"Person {i}"} for i in range(150)]
    created = db.table("contacts").insert(contacts).execute().data
    idents = [{"contact_id": c["id"], "source": "seed", "email": f"p{i}@x.co"}
              for i, c in enumerate(created)]
    db.table("contact_identities").insert(idents).execute()
    staged = []
    for i in range(150):
        for j, day in enumerate(("2026-05-01", "2026-05-02")):
            staged.append({"source": "smoke", "source_external_id": f"r{i}-{j}",
                           "email": f"p{i}@x.co", "kind": "message",
                           "channel": "telegram", "occurred_at": day,
                           "summary": f"touch {j} for {i}"})
    for k in range(0, len(staged), 200):
        db.table("staging_interactions").insert(staged[k:k + 200]).execute()

    r = runner.invoke(app, ["backfill", "--workers", "4"])
    assert r.exit_code == 0, r.output
    inter = db.table("interactions").select("source_external_id").eq(
        "source", "smoke").execute().data
    assert len(inter) == 300                                   # no losses
    assert len({i["source_external_id"] for i in inter}) == 300  # no dupes
    left = db.table("staging_interactions").select("id").eq(
        "match_status", "pending").execute().data
    assert left == []
    # every contact got the newer date + telegram tier via recompute
    sample = db.table("contacts").select("last_touchpoint_at,closeness_tier").eq(
        "id", created[0]["id"]).single().execute().data
    assert sample["last_touchpoint_at"] == "2026-05-02"
    assert sample["closeness_tier"] == "t1_irl_messaging"


def test_manual_log_channels_now_count_for_tier(db):
    """Declared broadening (spec §2.3): crm log'd channels become tier evidence
    the next time recompute touches the contact."""
    c = db.table("contacts").insert({"full_name": "Ada"}).execute().data[0]
    db.table("contact_identities").insert(
        {"contact_id": c["id"], "source": "seed", "email": "a@b.co"}).execute()
    r = runner.invoke(app, ["log", c["id"], "--kind", "call",
                            "--channel", "whatsapp", "--date", "2026-04-01"])
    assert r.exit_code == 0, r.output
    # under the OLD engine crm log never upgraded tier
    pre = db.table("contacts").select("closeness_tier").eq(
        "id", c["id"]).single().execute().data
    assert pre["closeness_tier"] == "none"
    # any backfill touching this contact recomputes from ALL interactions
    db.table("staging_interactions").insert(
        {"source": "smoke2", "source_external_id": "x1", "email": "a@b.co",
         "kind": "email", "channel": "email", "occurred_at": "2026-05-01"}).execute()
    runner.invoke(app, ["backfill"])
    post = db.table("contacts").select("closeness_tier").eq(
        "id", c["id"]).single().execute().data
    assert post["closeness_tier"] == "t1_irl_messaging"   # whatsapp evidence counted


def test_crash_between_insert_and_patch_recovers(db):
    """A killed run can leave the interaction inserted but the staging row
    stranded in 'claimed'. The rerun must reset the claim, take the bulk
    existing-hit refresh path, and end with exactly ONE interaction."""
    c = db.table("contacts").insert({"full_name": "Ada"}).execute().data[0]
    db.table("contact_identities").insert(
        {"contact_id": c["id"], "source": "seed", "email": "a@b.co"}).execute()
    db.table("staging_interactions").insert(
        {"source": "crashy", "source_external_id": "x1", "email": "a@b.co",
         "kind": "message", "channel": "telegram", "occurred_at": "2026-05-01",
         "summary": "crashed mid-page", "match_status": "claimed"}).execute()
    # the interaction landed before the crash
    db.table("interactions").insert(
        {"contact_id": c["id"], "kind": "message", "channel": "telegram",
         "occurred_at": "2026-05-01", "summary": "crashed mid-page",
         "logged_by": "rahul", "source": "crashy",
         "source_external_id": "x1"}).execute()
    r = runner.invoke(app, ["backfill"])
    assert r.exit_code == 0, r.output
    inter = db.table("interactions").select("id").eq("source", "crashy").execute().data
    assert len(inter) == 1                       # refreshed, not duplicated
    staged = db.table("staging_interactions").select("match_status").eq(
        "source", "crashy").execute().data
    assert staged[0]["match_status"] == "linked"
