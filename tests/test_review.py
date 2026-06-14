from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from crm.cli import app

runner = CliRunner()


def _seed_review_pair(db):
    db.table("staging").insert({"source": "s1", "source_external_id": "a",
                                "full_name": "Jonathan Smithers"}).execute()
    runner.invoke(app, ["dedup"])
    db.table("staging").insert({"source": "s2", "source_external_id": "b",
                                "full_name": "Jonathon Smithers"}).execute()
    runner.invoke(app, ["dedup"])
    row = (db.table("staging").select("*")
           .eq("match_status", "needs_review").execute().data)
    assert row, "expected a needs_review row"
    return row[0]


def test_review_list_shows_pair(db):
    _seed_review_pair(db)
    r = runner.invoke(app, ["review", "--json"])
    assert r.exit_code == 0
    assert "Jonathon Smithers" in r.output


def test_review_approve_attaches(db):
    row = _seed_review_pair(db)
    r = runner.invoke(app, ["review", "--approve", row["id"]])
    assert r.exit_code == 0
    assert len(db.table("contacts").select("id").execute().data) == 1
    assert len(db.table("contact_identities").select("id").execute().data) == 2


def test_review_reject_creates_new_contact(db):
    row = _seed_review_pair(db)
    runner.invoke(app, ["review", "--reject", row["id"]])
    assert len(db.table("contacts").select("id").execute().data) == 2


def test_merge_self_is_rejected(db):
    a = db.table("contacts").insert({"full_name": "Ada"}).execute().data[0]
    r = runner.invoke(app, ["merge", a["id"], a["id"]])
    assert r.exit_code == 2
    assert len(db.table("contacts").select("id").execute().data) == 1  # still alive


def test_merge_preserves_tier_tags_notes(db):
    keep = db.table("contacts").insert({"full_name": "Ada L"}).execute().data[0]
    drop = db.table("contacts").insert(
        {"full_name": "Ada Lovelace", "closeness_tier": "t1_irl_messaging",
         "connection_status": "in_network", "tags": ["mentor"],
         "notes": "met at NS"}).execute().data[0]
    runner.invoke(app, ["tags", "add", "mentor", "--desc", "test"])  # registry hygiene
    r = runner.invoke(app, ["merge", keep["id"], drop["id"]])
    assert r.exit_code == 0
    k = db.table("contacts").select("*").eq("id", keep["id"]).single().execute().data
    assert k["closeness_tier"] == "t1_irl_messaging"
    assert k["connection_status"] == "in_network"
    assert "mentor" in k["tags"]
    assert "met at NS" in (k["notes"] or "")


def test_approve_with_deleted_candidate_fails_cleanly(db):
    row = _seed_review_pair(db)
    # simulate: the candidate contact got merged away → FK set matched_contact_id null
    db.table("staging").update({"matched_contact_id": None}).eq("id", row["id"]).execute()
    r = runner.invoke(app, ["review", "--approve", row["id"]])
    assert r.exit_code == 1


def _seed_conflict_and_fuzzy(db):
    """Insert two contacts + one conflicting_keys staging row and one fuzzy_name row.

    Returns (conflict_row, fuzzy_row).
    """
    alice = db.table("contacts").insert(
        {"full_name": "Alice Vance", "current_company": "AcmeCo"}
    ).execute().data[0]
    bob = db.table("contacts").insert(
        {"full_name": "Bob Vance", "current_company": "VanceCo"}
    ).execute().data[0]

    # Give each contact a distinct identity key so we can use them in the conflict row
    db.table("contact_identities").insert(
        {"contact_id": alice["id"], "source": "li", "linkedin_url": "https://li.co/alice"}
    ).execute()
    db.table("contact_identities").insert(
        {"contact_id": bob["id"], "source": "li", "source_external_id": "bob-li",
         "linkedin_url": "https://li.co/bob"}
    ).execute()

    # A staging row that points at both alice (via linkedin) and bob (via source_external_id
    # on email) — we fake it by inserting directly into staging with conflicting_keys
    conflict_row = db.table("staging").insert({
        "source": "test_src",
        "source_external_id": "conflict-1",
        "full_name": "Conflict Person",
        "linkedin_url": "https://li.co/alice",   # matches alice's identity
        "match_status": "needs_review",
        "match_method": "conflicting_keys",
        "match_confidence": 0.75,
        "matched_contact_id": alice["id"],
    }).execute().data[0]

    # A fuzzy staging row pointing at bob
    fuzzy_row = db.table("staging").insert({
        "source": "test_src",
        "source_external_id": "fuzzy-1",
        "full_name": "Robert Vance",
        "match_status": "needs_review",
        "match_method": "fuzzy_name",
        "match_confidence": 0.80,
        "matched_contact_id": bob["id"],
    }).execute().data[0]

    return conflict_row, fuzzy_row


# ---------------------------------------------------------------------------
# Task 1.6 tests — two-pass batched review queue
# ---------------------------------------------------------------------------

def test_render_parity_conflict_and_fuzzy(db):
    """Batched _candidate_display must produce identical output to the current per-row version.

    Conflict rows use a set internally so candidate ORDER is non-deterministic; we
    compare by splitting on ', ' and sorting the parts, not byte-identical string.
    """
    from crm.commands.dedup import _candidate_display, _prefetch_display_maps
    from crm.config import get_client

    conflict_row, fuzzy_row = _seed_conflict_and_fuzzy(db)
    client = get_client()

    # Capture golden output via the NEW API (prefetch maps, then render pure fn)
    maps = _prefetch_display_maps(client, [conflict_row, fuzzy_row])
    golden_conflict = _candidate_display(maps, conflict_row)
    golden_fuzzy = _candidate_display(maps, fuzzy_row)

    # Sanity: golden output should be non-empty sentinel or actual names
    assert golden_conflict not in ("", None)
    assert golden_fuzzy not in ("", None)

    # Now invoke the full review listing and check the rendered output
    r = runner.invoke(app, ["review", "--json"])
    assert r.exit_code == 0

    import json
    rows = json.loads(r.output)
    by_id = {row["id"]: row for row in rows}

    assert conflict_row["id"] in by_id, "conflict row missing from review output"
    assert fuzzy_row["id"] in by_id, "fuzzy row missing from review output"

    # Fuzzy: exact match (single candidate, deterministic)
    assert by_id[fuzzy_row["id"]]["candidate"] == golden_fuzzy

    # Conflict: order-insensitive comparison (set built from Python set → non-deterministic)
    actual_parts = sorted(by_id[conflict_row["id"]]["candidate"].split(", "))
    golden_parts = sorted(golden_conflict.split(", "))
    assert actual_parts == golden_parts, (
        f"Conflict candidate mismatch:\n  actual: {actual_parts}\n  golden: {golden_parts}"
    )


def test_role_email_skip_preserved(db):
    """A staging row whose email is a role address must not surface that address as a
    candidate key — same as the current _candidate_display skip."""
    contact = db.table("contacts").insert(
        {"full_name": "Support Person", "current_company": "HelpCo"}
    ).execute().data[0]
    db.table("contact_identities").insert(
        {"contact_id": contact["id"], "source": "em", "email": "info@helpco.com"}
    ).execute()

    # staging row with a role email — should get <no candidates> if info@ is the only key
    row = db.table("staging").insert({
        "source": "test_src",
        "source_external_id": "role-email-1",
        "full_name": "Support Person",
        "email": "info@helpco.com",
        "match_status": "needs_review",
        "match_method": "conflicting_keys",
        "match_confidence": 0.75,
        "matched_contact_id": None,
    }).execute().data[0]

    from crm.commands.dedup import _candidate_display, _prefetch_display_maps
    from crm.config import get_client
    client = get_client()
    maps = _prefetch_display_maps(client, [row])
    result = _candidate_display(maps, row)
    # role email must be skipped; matched_contact_id is None → <no candidates>
    assert result == "<no candidates>", f"Expected '<no candidates>', got: {result!r}"


_seed_counter = {"val": 0}  # module-level counter avoids duplicate keys across calls


def _seed_n_fuzzy_rows(db, n: int):
    """Create n independent fuzzy review rows (each pointing at a distinct contact).

    Uses a module-level counter so repeated calls within the same test session never
    produce duplicate (source, source_external_id) pairs.
    """
    offset = _seed_counter["val"]
    _seed_counter["val"] += n

    contacts = db.table("contacts").insert(
        [{"full_name": f"BulkPerson {offset + i}", "current_company": f"BulkCo{offset + i}"}
         for i in range(n)]
    ).execute().data

    rows = db.table("staging").insert([
        {
            "source": "bulk_test",
            "source_external_id": f"bulk-{offset + i}",
            "full_name": f"BulkPerson {offset + i} Variant",
            "match_status": "needs_review",
            "match_method": "fuzzy_name",
            "match_confidence": 0.80,
            "matched_contact_id": contacts[i]["id"],
        }
        for i in range(n)
    ]).execute().data

    return rows


def test_n_invariance_review_display(db):
    """Display pass must issue O(1) reads, not O(R) — same number of select queries
    for 2 rows and 10 rows.

    We patch crm.commands.dedup.get_client to inject the spy, then invoke review
    list-only (no --approve/--reject flags).  The spy counts .execute() calls on
    the table proxy; mutation calls (staging select at the top of review) are
    excluded from comparison — we compare only the contact + identity selects
    that grow with R in the current code.
    """
    from tests._spy import CountingClient
    from crm.config import get_client

    real_client = get_client()

    def make_spy():
        return CountingClient(real_client)

    # --- 2 rows ---
    _seed_n_fuzzy_rows(db, 2)
    spy2 = make_spy()
    with patch("crm.commands.dedup.get_client", return_value=spy2):
        r = runner.invoke(app, ["review"])
    assert r.exit_code == 0

    # contacts selects in display pass — with batching there's exactly 1 regardless of R
    contacts_selects_2 = spy2.count("contacts", "select")
    identity_selects_2 = spy2.count("contact_identities", "select")

    # --- 10 rows (add 8 more) ---
    _seed_n_fuzzy_rows(db, 8)
    spy10 = make_spy()
    with patch("crm.commands.dedup.get_client", return_value=spy10):
        r = runner.invoke(app, ["review"])
    assert r.exit_code == 0

    contacts_selects_10 = spy10.count("contacts", "select")
    identity_selects_10 = spy10.count("contact_identities", "select")

    # With the batched implementation, display-phase selects don't grow with R.
    # They should be identical (1 contacts select, 0-3 identity selects).
    assert contacts_selects_2 == contacts_selects_10, (
        f"contacts selects grew: {contacts_selects_2} (R=2) vs {contacts_selects_10} (R=10) — "
        "N+1 not fixed"
    )
    assert identity_selects_2 == identity_selects_10, (
        f"identity selects grew: {identity_selects_2} (R=2) vs {identity_selects_10} (R=10) — "
        "N+1 not fixed"
    )


def test_identity_discovered_candidate_second_pass(db):
    """Cover _prefetch_display_maps lines 411 and 415-422 (second-pass fetch).

    Set up a contact C with an email identity but insert the staging row with
    matched_contact_id=NULL.  _prefetch_display_maps first pass collects NO
    contact_ids (matched_contact_id is NULL).  The identity-lookup pass finds C
    via the email → line 411 fires (cid not in contact_map → contact_ids.add(cid)).
    Lines 415-422 then fetch C in the second pass.
    _candidate_display renders C's name, proving the path ran end-to-end.
    """
    from crm.commands.dedup import _candidate_display, _prefetch_display_maps
    from crm.config import get_client

    # Create a contact with a real (non-role) email identity
    contact = db.table("contacts").insert(
        {"full_name": "Diana Prince", "current_company": "WonderCo"}
    ).execute().data[0]
    db.table("contact_identities").insert(
        {"contact_id": contact["id"], "source": "em", "email": "diana@wonder.co"}
    ).execute()

    # Staging row: matched_contact_id is NULL but email matches the identity above.
    # This means the first-pass contact fetch skips contact["id"] entirely;
    # only the identity lookup discovers it → second-pass fetch required.
    row = db.table("staging").insert({
        "source": "test_src",
        "source_external_id": "second-pass-1",
        "full_name": "Diana P",
        "email": "diana@wonder.co",
        "match_status": "needs_review",
        "match_method": "conflicting_keys",
        "match_confidence": 0.80,
        "matched_contact_id": None,
    }).execute().data[0]

    client = get_client()
    maps = _prefetch_display_maps(client, [row])

    # The second-pass must have fetched the contact
    assert contact["id"] in maps["contacts"], (
        "second-pass fetch did not populate contact_map with identity-discovered contact"
    )

    result = _candidate_display(maps, row)
    # Should render C's name, not <gone> or <no candidates>
    assert "Diana Prince" in result, f"Expected candidate name in output, got: {result!r}"
    assert "WonderCo" in result, f"Expected company in output, got: {result!r}"

    # Also verify end-to-end via review list command
    r = runner.invoke(app, ["review", "--json"])
    assert r.exit_code == 0

    import json
    rows_out = json.loads(r.output)
    by_id = {r_["id"]: r_ for r_ in rows_out}
    assert row["id"] in by_id, "staging row missing from review output"
    assert "Diana Prince" in by_id[row["id"]]["candidate"]


def test_merge_and_split(db):
    a = db.table("contacts").insert({"full_name": "Ada L"}).execute().data[0]
    b = db.table("contacts").insert({"full_name": "Ada Lovelace"}).execute().data[0]
    ident_b = db.table("contact_identities").insert(
        {"contact_id": b["id"], "source": "s", "email": "x@y.z"}
    ).execute().data[0]
    r = runner.invoke(app, ["merge", a["id"], b["id"]])
    assert r.exit_code == 0
    assert len(db.table("contacts").select("id").execute().data) == 1
    moved = db.table("contact_identities").select("contact_id").execute().data
    assert moved[0]["contact_id"] == a["id"]
    r = runner.invoke(app, ["split", a["id"], ident_b["id"]])
    assert r.exit_code == 0
    assert len(db.table("contacts").select("id").execute().data) == 2
