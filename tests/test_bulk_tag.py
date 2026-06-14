# tests/test_bulk_tag.py
"""Behavioral tests for the bulk_add_tag RPC (migration 0009).

Run against the local Supabase stack via the `db` fixture.
Always `supabase db reset` before running to apply migrations fresh.
"""
import uuid


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _contact(db, name, **kw):
    defaults = {
        "connection_status": "contact_on_file",
        "closeness_tier": "none",
    }
    return db.table("contacts").insert({**defaults, "full_name": name, **kw}).execute().data[0]


def _tags(db, contact_id) -> list[str]:
    """Fetch the current tags array for a given contact id."""
    row = db.table("contacts").select("tags").eq("id", contact_id).single().execute().data
    return row["tags"]


def _rpc(db, tag: str, ids: list[str]) -> list[str]:
    return db.rpc("bulk_add_tag", {"p_tag": tag, "p_ids": ids}).execute().data


# ---------------------------------------------------------------------------
# idempotent: returns only newly-affected ids
# ---------------------------------------------------------------------------

def test_bulk_add_tag_idempotent_returns_newly_affected(db):
    """Calling bulk_add_tag with p_ids including one already-tagged contact
    returns ONLY the 2 newly-affected ids; the pre-tagged one is excluded."""
    pre = _contact(db, "Pre-Tagged", tags=["vip"])
    c1  = _contact(db, "Contact A")
    c2  = _contact(db, "Contact B")

    affected = _rpc(db, "vip", [pre["id"], c1["id"], c2["id"]])

    # only the two fresh contacts should be returned
    assert set(affected) == {c1["id"], c2["id"]}
    # the pre-tagged one must NOT appear in the result
    assert pre["id"] not in affected


def test_bulk_add_tag_newly_tagged_contacts_have_tag(db):
    """The two contacts that lacked 'vip' must have it after the call."""
    c1 = _contact(db, "Fresh One")
    c2 = _contact(db, "Fresh Two")

    _rpc(db, "vip", [c1["id"], c2["id"]])

    assert "vip" in _tags(db, c1["id"])
    assert "vip" in _tags(db, c2["id"])


def test_bulk_add_tag_pre_tagged_unchanged_no_dup(db):
    """The already-tagged contact must still carry exactly one 'vip' — no duplicate."""
    pre = _contact(db, "Already VIP", tags=["vip"])
    c1  = _contact(db, "New Guy")

    _rpc(db, "vip", [pre["id"], c1["id"]])

    tags = _tags(db, pre["id"])
    assert tags.count("vip") == 1, f"duplicate 'vip' found: {tags}"


# ---------------------------------------------------------------------------
# sort order
# ---------------------------------------------------------------------------

def test_bulk_add_tag_result_is_sorted(db):
    """After adding 'mid' to a contact with tags ['zeta','alpha'],
    the resulting tags array must be ['alpha','mid','zeta'] (sorted asc)."""
    c = _contact(db, "Sort Test", tags=["zeta", "alpha"])

    _rpc(db, "mid", [c["id"]])

    assert _tags(db, c["id"]) == ["alpha", "mid", "zeta"]


# ---------------------------------------------------------------------------
# empty p_ids
# ---------------------------------------------------------------------------

def test_bulk_add_tag_empty_ids_returns_empty(db):
    """Passing an empty p_ids list returns [] and mutates nothing."""
    c = _contact(db, "Untouched")
    before = _tags(db, c["id"])

    result = _rpc(db, "vip", [])

    assert result == []
    assert _tags(db, c["id"]) == before


# ---------------------------------------------------------------------------
# contact with default empty tags
# ---------------------------------------------------------------------------

def test_bulk_add_tag_empty_tags_gets_tag(db):
    """A contact with the default tags='{}'  gets ['vip'] after the call."""
    c = _contact(db, "Empty Tags")  # tags defaults to '{}'

    _rpc(db, "vip", [c["id"]])

    assert _tags(db, c["id"]) == ["vip"]
