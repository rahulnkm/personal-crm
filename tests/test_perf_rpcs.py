# tests/test_perf_rpcs.py
"""Behavioral tests for the three perf RPCs introduced in 0006_perf_rpcs.sql.

All tests run against the local Supabase stack via the `db` fixture (supabase
client with service_role key, data tables truncated between tests).
"""
import uuid
from datetime import date


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _contact(db, name, **kw):
    return db.table("contacts").insert({"full_name": name, **kw}).execute().data[0]


def _interaction_payload(contact_id, *, source_external_id, kind="email",
                         channel="email", occurred_at="2024-01-01",
                         summary="hi", event_id=None, source="test"):
    row = {
        "contact_id": str(contact_id),
        "kind": kind,
        "channel": channel,
        "occurred_at": occurred_at,
        "summary": summary,
        "logged_by": "rahul",
        "source": source,
        "source_external_id": source_external_id,
    }
    if event_id is not None:
        row["event_id"] = str(event_id)
    return row


# ---------------------------------------------------------------------------
# bulk_upsert_interactions
# ---------------------------------------------------------------------------

def test_bulk_upsert_interactions_insert_new(db):
    """Fresh row is inserted; RPC returns [] (no contact_id moved)."""
    c = _contact(db, "Alice Test")
    ext_id = f"ext-{uuid.uuid4()}"
    payload = [_interaction_payload(c["id"], source_external_id=ext_id)]
    result = db.rpc("bulk_upsert_interactions", {"payload": payload}).execute().data
    assert result == []
    rows = db.table("interactions").select("*").eq("source_external_id", ext_id).execute().data
    assert len(rows) == 1
    assert rows[0]["summary"] == "hi"


def test_bulk_upsert_interactions_refresh_summary(db):
    """Re-inserting same source_external_id updates summary; still one row."""
    c = _contact(db, "Bob Test")
    ext_id = f"ext-{uuid.uuid4()}"
    p1 = [_interaction_payload(c["id"], source_external_id=ext_id, summary="first")]
    db.rpc("bulk_upsert_interactions", {"payload": p1}).execute()

    p2 = [_interaction_payload(c["id"], source_external_id=ext_id, summary="second")]
    db.rpc("bulk_upsert_interactions", {"payload": p2}).execute()

    rows = db.table("interactions").select("summary").eq("source_external_id", ext_id).execute().data
    assert len(rows) == 1
    assert rows[0]["summary"] == "second"


def test_bulk_upsert_interactions_kind_preserved_on_refresh(db):
    """kind column must NOT change on a refresh — it stays as first inserted."""
    c = _contact(db, "Carol Test")
    ext_id = f"ext-{uuid.uuid4()}"
    # insert with kind=meeting
    p1 = [_interaction_payload(c["id"], source_external_id=ext_id, kind="meeting")]
    db.rpc("bulk_upsert_interactions", {"payload": p1}).execute()

    # refresh with kind=call — kind must be PRESERVED as 'meeting'
    p2 = [_interaction_payload(c["id"], source_external_id=ext_id, kind="call",
                               summary="updated")]
    db.rpc("bulk_upsert_interactions", {"payload": p2}).execute()

    rows = db.table("interactions").select("kind").eq("source_external_id", ext_id).execute().data
    assert rows[0]["kind"] == "meeting"


def test_bulk_upsert_interactions_event_id_flips_null_to_uuid(db):
    """event_id may start null and be set on a subsequent refresh."""
    c = _contact(db, "Dave Test")
    ext_id = f"ext-{uuid.uuid4()}"
    # insert without event_id
    db.rpc("bulk_upsert_interactions",
           {"payload": [_interaction_payload(c["id"], source_external_id=ext_id)]}).execute()

    # create an event, then refresh with it
    evt = db.table("events").insert(
        {"name": "Perf Conf", "occurred_at": "2024-03-01", "created_by": "rahul"}
    ).execute().data[0]

    p2 = [_interaction_payload(c["id"], source_external_id=ext_id,
                               event_id=evt["id"], summary="after event")]
    db.rpc("bulk_upsert_interactions", {"payload": p2}).execute()

    rows = db.table("interactions").select("event_id").eq("source_external_id", ext_id).execute().data
    assert rows[0]["event_id"] == evt["id"]


def test_bulk_upsert_interactions_contact_id_flip_returns_prior(db):
    """When a refresh moves contact_id A→B, RPC returns A (the abandoned contact)."""
    a = _contact(db, "A Test")
    b = _contact(db, "B Test")
    ext_id = f"ext-{uuid.uuid4()}"

    # insert pointing at A
    db.rpc("bulk_upsert_interactions",
           {"payload": [_interaction_payload(a["id"], source_external_id=ext_id)]}).execute()

    # refresh pointing at B — expect A's id back
    result = db.rpc("bulk_upsert_interactions",
                    {"payload": [_interaction_payload(b["id"], source_external_id=ext_id)]}).execute().data
    assert a["id"] in result


def test_bulk_upsert_interactions_no_move_returns_empty(db):
    """Refresh that doesn't move contact_id returns []."""
    c = _contact(db, "E Test")
    ext_id = f"ext-{uuid.uuid4()}"
    db.rpc("bulk_upsert_interactions",
           {"payload": [_interaction_payload(c["id"], source_external_id=ext_id)]}).execute()

    result = db.rpc("bulk_upsert_interactions",
                    {"payload": [_interaction_payload(c["id"], source_external_id=ext_id,
                                                      summary="same contact")]}).execute().data
    assert result == []


def test_bulk_upsert_interactions_empty_payload_noop(db):
    """Empty payload list must not error and returns []."""
    result = db.rpc("bulk_upsert_interactions", {"payload": []}).execute().data
    assert result == []


# ---------------------------------------------------------------------------
# bulk_bump_last_touchpoint
# ---------------------------------------------------------------------------

def test_bulk_bump_last_touchpoint_null_bumps(db):
    """Contact with null last_touchpoint_at gets bumped."""
    c = _contact(db, "F Test")
    assert c.get("last_touchpoint_at") is None

    db.rpc("bulk_bump_last_touchpoint", {
        "p_ids": [c["id"]],
        "p_occurred": "2024-06-01",
        "p_channel": "email",
        "p_topic": "hello",
    }).execute()

    row = db.table("contacts").select(
        "last_touchpoint_at,last_touchpoint_channel,last_touchpoint_topic"
    ).eq("id", c["id"]).single().execute().data
    assert row["last_touchpoint_at"] == "2024-06-01"
    assert row["last_touchpoint_channel"] == "email"
    assert row["last_touchpoint_topic"] == "hello"


def test_bulk_bump_last_touchpoint_older_bumps(db):
    """Contact with an older date gets bumped to the newer date."""
    c = _contact(db, "G Test", last_touchpoint_at="2023-01-01",
                 last_touchpoint_channel="sms", last_touchpoint_topic="old")

    db.rpc("bulk_bump_last_touchpoint", {
        "p_ids": [c["id"]],
        "p_occurred": "2024-01-01",
        "p_channel": "call",
        "p_topic": "new",
    }).execute()

    row = db.table("contacts").select("last_touchpoint_at").eq(
        "id", c["id"]).single().execute().data
    assert row["last_touchpoint_at"] == "2024-01-01"


def test_bulk_bump_last_touchpoint_equal_is_noop(db):
    """Equal date must NOT update (< guard, not <=)."""
    c = _contact(db, "H Test", last_touchpoint_at="2024-06-01",
                 last_touchpoint_channel="email", last_touchpoint_topic="original")

    db.rpc("bulk_bump_last_touchpoint", {
        "p_ids": [c["id"]],
        "p_occurred": "2024-06-01",
        "p_channel": "call",
        "p_topic": "should not overwrite",
    }).execute()

    row = db.table("contacts").select(
        "last_touchpoint_channel,last_touchpoint_topic"
    ).eq("id", c["id"]).single().execute().data
    assert row["last_touchpoint_channel"] == "email"
    assert row["last_touchpoint_topic"] == "original"


def test_bulk_bump_last_touchpoint_newer_is_noop(db):
    """Newer existing date must NOT be overwritten by an older p_occurred."""
    c = _contact(db, "I Test", last_touchpoint_at="2025-01-01",
                 last_touchpoint_channel="meeting", last_touchpoint_topic="future")

    db.rpc("bulk_bump_last_touchpoint", {
        "p_ids": [c["id"]],
        "p_occurred": "2024-01-01",
        "p_channel": "email",
        "p_topic": "past",
    }).execute()

    row = db.table("contacts").select("last_touchpoint_at").eq(
        "id", c["id"]).single().execute().data
    assert row["last_touchpoint_at"] == "2025-01-01"


def test_bulk_bump_last_touchpoint_empty_ids_noop(db):
    """Empty p_ids list must not error."""
    db.rpc("bulk_bump_last_touchpoint", {
        "p_ids": [],
        "p_occurred": "2024-06-01",
        "p_channel": "email",
        "p_topic": "noop",
    }).execute()


# ---------------------------------------------------------------------------
# crm_stats
# ---------------------------------------------------------------------------

def test_crm_stats_returns_jsonb_object(db):
    """crm_stats always returns a single jsonb object with expected top-level keys."""
    result = db.rpc("crm_stats", {}).execute().data
    # PostgREST unwraps a single-row stable function into a list with one element
    assert isinstance(result, (dict, list))
    stats = result if isinstance(result, dict) else result[0]
    for key in ("connection_status", "closeness_tier", "staging", "touchpoints", "contacts_total"):
        assert key in stats


def test_crm_stats_known_distribution(db):
    """Seed a known distribution; assert counts match hand-counted values."""
    # contacts: 2 in_network + 1 contact_on_file; 1 t1 + 2 none
    _contact(db, "J Test", connection_status="in_network", closeness_tier="t1_irl_messaging")
    _contact(db, "K Test", connection_status="in_network", closeness_tier="none")
    _contact(db, "L Test", connection_status="contact_on_file", closeness_tier="none")

    # staging: 2 pending + 1 auto_matched
    db.table("staging").insert([
        {"source": "s", "source_external_id": "x1", "full_name": "P1", "match_status": "pending"},
        {"source": "s", "source_external_id": "x2", "full_name": "P2", "match_status": "pending"},
        {"source": "s", "source_external_id": "x3", "full_name": "P3", "match_status": "auto_matched"},
    ]).execute()

    # staging_interactions: 3 pending + 1 linked
    db.table("staging_interactions").insert([
        {"source": "s", "source_external_id": "si1", "kind": "email",
         "match_status": "pending"},
        {"source": "s", "source_external_id": "si2", "kind": "email",
         "match_status": "pending"},
        {"source": "s", "source_external_id": "si3", "kind": "email",
         "match_status": "pending"},
        {"source": "s", "source_external_id": "si4", "kind": "email",
         "match_status": "linked"},
    ]).execute()

    result = db.rpc("crm_stats", {}).execute().data
    stats = result if isinstance(result, dict) else result[0]

    # connection_status
    assert stats["connection_status"]["in_network"] == 2
    assert stats["connection_status"]["contact_on_file"] == 1

    # closeness_tier
    assert stats["closeness_tier"]["t1_irl_messaging"] == 1
    assert stats["closeness_tier"]["none"] == 2

    # staging
    assert stats["staging"]["pending"] == 2
    assert stats["staging"]["auto_matched"] == 1

    # touchpoints
    assert stats["touchpoints"]["pending"] == 3
    assert stats["touchpoints"]["linked"] == 1

    # contacts_total
    assert stats["contacts_total"] == 3


def test_crm_stats_counts_are_ints(db):
    """Counts must be Python ints (3, not 3.0) — verifying ::int cast in SQL."""
    _contact(db, "M Test")
    _contact(db, "N Test")
    _contact(db, "O Test")

    result = db.rpc("crm_stats", {}).execute().data
    stats = result if isinstance(result, dict) else result[0]

    total = stats["contacts_total"]
    assert total == 3
    assert isinstance(total, int)

    # also check a bucket inside a sub-object
    for sub_key in ("connection_status", "closeness_tier"):
        for v in stats[sub_key].values():
            assert isinstance(v, int), f"{sub_key} bucket is {type(v)}, not int"


def test_crm_stats_absent_bucket_not_in_sub_object(db):
    """Buckets with zero rows must simply be absent — not present as 0."""
    # Only contact_on_file contacts → in_network must not appear
    _contact(db, "P Test", connection_status="contact_on_file")

    result = db.rpc("crm_stats", {}).execute().data
    stats = result if isinstance(result, dict) else result[0]

    # in_network has 0 rows so it must be absent from the dict
    assert "in_network" not in stats["connection_status"]


def test_crm_stats_mixed_empty(db):
    """One source table empty, others populated — no error, empty sub-object is '{}'."""
    # Populate contacts only; staging and staging_interactions empty
    _contact(db, "Q Test")

    result = db.rpc("crm_stats", {}).execute().data
    stats = result if isinstance(result, dict) else result[0]

    assert stats["contacts_total"] == 1
    # staging and touchpoints must be empty dicts (coalesce '{}')
    assert stats["staging"] == {}
    assert stats["touchpoints"] == {}
