# tests/test_recompute.py
"""Direct tests of backfill_recompute_contacts (migration 0004) and the
events uniqueness guard. The RPC is the single writer of contacts'
denormalized fields under the parallel design."""
import pytest
from postgrest.exceptions import APIError

from crm.closeness import CHANNEL_TIER, TIER_RANK


def _rpc(db, ids):
    db.rpc("backfill_recompute_contacts",
           {"contact_ids": ids, "channel_tier": CHANNEL_TIER,
            "tier_rank": TIER_RANK}).execute()


def _contact(db, name, **kw):
    return db.table("contacts").insert({"full_name": name, **kw}).execute().data[0]


def _interaction(db, cid, **kw):
    row = {"contact_id": cid, "kind": "message", "logged_by": "rahul"}
    row.update(kw)
    return db.table("interactions").insert(row).execute().data[0]


def test_recompute_latest_dated_wins_and_best_tier(db):
    c = _contact(db, "Ada")
    _interaction(db, c["id"], channel="imessage", occurred_at="2026-01-01",
                 summary="old imessage")
    _interaction(db, c["id"], channel="twitter", occurred_at="2026-05-01",
                 summary="newer tweet")
    _interaction(db, c["id"], channel="email", occurred_at=None,
                 summary="undated — must never become last touchpoint")
    _rpc(db, [c["id"]])
    fresh = db.table("contacts").select("*").eq("id", c["id"]).single().execute().data
    # last touchpoint = LATEST DATED interaction (NULLs excluded)
    assert fresh["last_touchpoint_at"] == "2026-05-01"
    assert fresh["last_touchpoint_channel"] == "twitter"
    assert fresh["last_touchpoint_topic"] == "newer tweet"
    # tier = BEST evidence across all channels (imessage → t1), independent of recency
    assert fresh["closeness_tier"] == "t1_irl_messaging"


def test_recompute_never_downgrades(db):
    c = _contact(db, "Ada", closeness_tier="t1_irl_messaging")
    _interaction(db, c["id"], channel="twitter", occurred_at="2026-05-01")
    _rpc(db, [c["id"]])
    fresh = db.table("contacts").select("closeness_tier").eq(
        "id", c["id"]).single().execute().data
    assert fresh["closeness_tier"] == "t1_irl_messaging"


def test_recompute_ignores_unknown_channels_and_untouched_contacts(db):
    c = _contact(db, "Ada")
    other = _contact(db, "Untouched", closeness_tier="t2_dm",
                     last_touchpoint_at="2020-01-01")
    _interaction(db, c["id"], channel="carrier-pigeon", occurred_at="2026-05-01")
    _rpc(db, [c["id"]])
    fresh = db.table("contacts").select("*").eq("id", c["id"]).single().execute().data
    assert fresh["closeness_tier"] == "none"            # unknown channel = no evidence
    assert fresh["last_touchpoint_at"] == "2026-05-01"  # but it IS a dated touchpoint
    untouched = db.table("contacts").select("*").eq("id", other["id"]).single().execute().data
    assert untouched["last_touchpoint_at"] == "2020-01-01"  # not in ids → untouched


def test_events_backfill_unique_index_raises_23505(db):
    db.table("events").insert({"name": "NS dinner", "occurred_at": "2026-05-20",
                               "source": "backfill", "created_by": "rahul"}).execute()
    with pytest.raises(APIError) as exc:
        db.table("events").insert({"name": "NS dinner", "occurred_at": "2026-05-20",
                                   "source": "backfill", "created_by": "rahul"}).execute()
    assert exc.value.code == "23505"   # verifies supabase-py surfaces SQLSTATE
    # NULL-dated duplicates are also blocked (coalesce expression index)
    db.table("events").insert({"name": "No date", "source": "backfill",
                               "created_by": "rahul"}).execute()
    with pytest.raises(APIError):
        db.table("events").insert({"name": "No date", "source": "backfill",
                                   "created_by": "rahul"}).execute()
    # manual events are NOT constrained
    for _ in range(2):
        db.table("events").insert({"name": "Manual dup", "occurred_at": "2026-05-20",
                                   "source": "manual", "created_by": "rahul"}).execute()
