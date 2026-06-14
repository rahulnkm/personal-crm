import uuid

from typer.testing import CliRunner

from crm.cli import app
from crm.commands.log import _bump_last_touchpoint_bulk
from tests._spy import CountingClient

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


# ── Task 1.4 new tests ────────────────────────────────────────────────────────

def test_event_add_batched_interactions(db):
    """Multiple participants → ONE batched interactions insert (all get a row)."""
    a, b, c = _seed(db, "Ada"), _seed(db, "Grace"), _seed(db, "Turing")
    spy = CountingClient(db)
    from crm.commands import log as log_mod
    orig = log_mod.get_client

    log_mod.get_client = lambda: spy
    try:
        r = runner.invoke(app, ["event", "add", "Hack night",
                                "--date", "2026-06-01",
                                "--participants", f"{a['id']},{b['id']},{c['id']}"])
    finally:
        log_mod.get_client = orig

    assert r.exit_code == 0, r.output
    inter = db.table("interactions").select("contact_id").execute().data
    assert len(inter) == 3
    ids_got = {row["contact_id"] for row in inter}
    assert ids_got == {a["id"], b["id"], c["id"]}
    # Must be exactly 1 interactions insert (batched), not 3 separate ones
    assert spy.count("interactions", "insert") == 1


def test_event_add_bump_correctness(db):
    """After event_add, last_touchpoint_at is set for all participants."""
    a, b = _seed(db, "Ada"), _seed(db, "Grace")
    r = runner.invoke(app, ["event", "add", "Summit",
                            "--date", "2026-05-10",
                            "--participants", f"{a['id']},{b['id']}"])
    assert r.exit_code == 0, r.output
    for contact_id in [a["id"], b["id"]]:
        row = db.table("contacts").select("last_touchpoint_at,last_touchpoint_channel,last_touchpoint_topic") \
            .eq("id", contact_id).single().execute().data
        assert row["last_touchpoint_at"] == "2026-05-10"
        assert row["last_touchpoint_channel"] == "irl"
        assert row["last_touchpoint_topic"] == "Summit"


def test_event_add_equal_date_no_op(db):
    """A participant whose last_touchpoint_at already equals the event date is NOT changed."""
    a = _seed(db, "Ada")
    # Pre-seed a touchpoint at the same date with different channel/topic
    db.table("contacts").update({
        "last_touchpoint_at": "2026-06-01",
        "last_touchpoint_channel": "telegram",
        "last_touchpoint_topic": "existing topic",
    }).eq("id", a["id"]).execute()

    r = runner.invoke(app, ["event", "add", "New Event",
                            "--date", "2026-06-01",
                            "--participants", a["id"]])
    assert r.exit_code == 0, r.output

    row = db.table("contacts").select(
        "last_touchpoint_at,last_touchpoint_channel,last_touchpoint_topic"
    ).eq("id", a["id"]).single().execute().data
    # Equal-date → server-side no-op; original values preserved
    assert row["last_touchpoint_at"] == "2026-06-01"
    assert row["last_touchpoint_channel"] == "telegram"
    assert row["last_touchpoint_topic"] == "existing topic"


def test_event_add_none_date_no_bump(db):
    """None-date event → no bump at all; interactions still inserted with null occurred_at."""
    a = _seed(db, "Ada")
    r = runner.invoke(app, ["event", "add", "Undated Event",
                            "--participants", a["id"]])
    assert r.exit_code == 0, r.output

    inter = db.table("interactions").select("occurred_at").execute().data
    assert len(inter) == 1
    assert inter[0]["occurred_at"] is None

    row = db.table("contacts").select("last_touchpoint_at").eq("id", a["id"]).single().execute().data
    assert row["last_touchpoint_at"] is None


def test_event_add_unknown_uuid_participant_writes_nothing(db):
    """A well-formed uuid not in contacts → Exit(1) and NO event/interactions written."""
    a = _seed(db, "Ada")
    ghost_uuid = str(uuid.uuid4())  # valid format, non-existent contact
    r = runner.invoke(app, ["event", "add", "Ghost Reunion",
                            "--date", "2026-06-01",
                            "--participants", f"{a['id']},{ghost_uuid}"])
    assert r.exit_code == 1
    assert db.table("events").select("id").execute().data == []
    assert db.table("interactions").select("id").execute().data == []


def test_log_single_still_works(db):
    """Regression: single `log` command still functions after refactor."""
    c = _seed(db, "Grace Hopper")
    r = runner.invoke(app, ["log", c["id"], "--kind", "call",
                            "--channel", "phone", "--date", "2026-06-10",
                            "--summary", "quick catch-up"])
    assert r.exit_code == 0, r.output
    inter = db.table("interactions").select("kind,channel,occurred_at").execute().data
    assert len(inter) == 1
    assert inter[0]["kind"] == "call"
    assert inter[0]["channel"] == "phone"
    assert inter[0]["occurred_at"] == "2026-06-10"
    row = db.table("contacts").select("last_touchpoint_at").eq("id", c["id"]).single().execute().data
    assert row["last_touchpoint_at"] == "2026-06-10"


def test_event_add_n_participants_bounded_rpc_calls(db):
    """N participants → 1 interactions insert-batch + RPC calls bounded by chunks."""
    import crm.bulk as bulk_mod
    from crm.commands import log as log_mod

    contacts = [_seed(db, f"Person {i}") for i in range(5)]
    ids = ",".join(c["id"] for c in contacts)

    orig_chunk = bulk_mod.CHUNK
    orig_get_client = log_mod.get_client
    spy = CountingClient(db)
    bulk_mod.CHUNK = 2  # force chunking with 5 contacts → ceil(5/2) = 3 RPC calls
    log_mod.get_client = lambda: spy
    try:
        r = runner.invoke(app, ["event", "add", "Big Meetup",
                                "--date", "2026-06-01",
                                "--participants", ids])
    finally:
        bulk_mod.CHUNK = orig_chunk
        log_mod.get_client = orig_get_client

    assert r.exit_code == 0, r.output
    # Exactly 1 batched interactions insert (not 5 individual ones)
    assert spy.count("interactions", "insert") == 1
    # RPC calls = ceil(5/2) = 3, not 5 (not per-participant)
    rpc_calls = spy.rpc_count("bulk_bump_last_touchpoint")
    assert rpc_calls == 3  # ceil(5 / CHUNK=2)
