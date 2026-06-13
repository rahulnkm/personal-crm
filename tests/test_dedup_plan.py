"""Equivalence tests: build_plan must produce the SAME per-row verdict as the
sequential engine (crm/commands/dedup.py find_candidates + classify), via
in-order replay within each cluster.  No writes — only the plan list is checked.
"""
from crm.config import get_client
from crm.dedup_plan import _union_by_existing_contact, build_plan


def _stage(db, **f):
    row = {"source": f.pop("source", "s"),
           "source_external_id": f.get("source_external_id", f["full_name"]),
           "match_status": "pending"}
    row.update(f)
    return db.table("staging").insert(row).execute().data[0]


def _plan(db):
    pending = db.table("staging").select("*").eq("match_status", "pending").order(
        "imported_at").execute().data
    return {p["source_external_id"]: p for p in build_plan(get_client(), pending)}


def test_plan_review_band_does_not_merge(db):
    # THE 4th-review bug: review-band-only name edge must NOT auto-merge.
    _stage(db, full_name="Robert Smith", email="r@x.com", source_external_id="a")
    _stage(db, full_name="Robart Smith", email="b@y.com", source_external_id="b")
    v = _plan(db)
    assert v["a"]["match_status"] == "merged"          # own contact (create)
    assert v["b"]["match_status"] == "needs_review"    # queued, NOT attached


def test_plan_transitive_cut_point(db):
    _stage(db, full_name="Robert Smith", email="r@x.com", source_external_id="A")
    _stage(db, full_name="Robert Smith", email="r@x.com", source_external_id="B")  # same email
    _stage(db, full_name="Robart Smith", email="c@c.co", source_external_id="C")   # review name
    _stage(db, full_name="Zenith Quux", phone="+15550001111", source_external_id="D")
    v = _plan(db)
    assert v["A"]["match_status"] == "merged"
    assert v["B"]["match_status"] == "auto_matched" and v["B"]["match_method"] == "exact_email"
    assert v["C"]["match_status"] == "needs_review"    # review-band, not a bridge
    assert v["D"]["match_status"] == "merged"          # own contact


def test_plan_same_cluster_id_for_connected(db):
    _stage(db, full_name="Ada", email="a@b.co", source_external_id="1")
    _stage(db, full_name="Ada", email="a@b.co", source_external_id="2")
    v = _plan(db)
    assert v["1"]["cluster_id"] == v["2"]["cluster_id"]


def test_union_fuzzy_auto_common_target_merges_clusters():
    """Regression (cross-thread fill race): two rows with NO exact keys that each
    fuzzy-AUTO-match (score ≥ AUTO_MERGE 0.92) the SAME existing contact X — but
    are dissimilar to EACH OTHER, so no clustering name edge joins them — land in
    DIFFERENT clusters. Before the fix, _union_by_existing_contact only unioned on
    exact-key targets, so these two clusters stayed split → two threads both filled
    contact X → read-modify-write race lost one fill.

    Unit-style: construct the union inputs directly (the geometric near-miss —
    both ≥0.92 to X yet <0.55 to each other — is hard to realize with real names),
    and assert the two clusters get merged into one.
    """
    contact_x = "00000000-0000-0000-0000-00000000000X"
    r1 = {"id": "r1"}
    r2 = {"id": "r2"}
    rows = [r1, r2]
    # Each row in its own cluster (no shared name edge → no clustering merge).
    clusters = {"c1": [r1], "c2": [r2]}
    # No exact-key hits for either row.
    exact = {"r1": [], "r2": []}
    # Both fuzzy-auto-match the SAME existing contact X.
    fuzzy = {"r1": (contact_x, 0.95), "r2": (contact_x, 0.95)}

    merged = _union_by_existing_contact(clusters, rows, exact, fuzzy)

    # Both rows must end up in ONE merged cluster (one write-isolation unit for X).
    assert len(merged) == 1, f"expected clusters merged into 1, got {len(merged)}"
    member_ids = {r["id"] for ms in merged.values() for r in ms}
    assert member_ids == {"r1", "r2"}


def test_union_fuzzy_review_does_not_merge():
    """Counterpart: a sub-auto fuzzy hit (<0.92) is NOT attached, so it must NOT
    contribute a union edge — the two clusters stay separate."""
    contact_x = "00000000-0000-0000-0000-00000000000X"
    r1 = {"id": "r1"}
    r2 = {"id": "r2"}
    rows = [r1, r2]
    clusters = {"c1": [r1], "c2": [r2]}
    exact = {"r1": [], "r2": []}
    # Both fuzzy-hit X but below AUTO_MERGE → review band → not attached.
    fuzzy = {"r1": (contact_x, 0.70), "r2": (contact_x, 0.70)}

    merged = _union_by_existing_contact(clusters, rows, exact, fuzzy)

    assert len(merged) == 2, f"sub-auto fuzzy must not union; got {len(merged)} clusters"


def test_conflicting_keys_ref_matches_field_order(db):
    """Regression: conflicting_keys matched_ref must equal the FIRST ref in field
    order (email → linkedin_url → phone), mirroring find_candidates' next(iter(hits)).

    Setup:
      - Existing contact P: phone +15550009999  (in DB before staging)
      - Row R1: creates member M via email m@x.co  (earlier imported_at)
      - Row R2: email m@x.co AND phone +15550009999  (later imported_at, same cluster)
        → email hits M (resolved member), phone hits P (existing) → 2 distinct → conflicting_keys
        → field order: email comes before phone, so matched_ref MUST be M's create_key, not P.

    Verify against the sequential engine:
      find_candidates iterates email first — hits M, then phone — hits P; len(hits) > 1
      → returns cid of M (next(iter(hits))).  Plan must agree.
    """
    from crm.config import get_client

    client = get_client()

    # Create existing contact P with phone +15550009999 in the live DB
    p_contact = (client.table("contacts")
                 .insert({"full_name": "Existing Person"})
                 .execute().data[0])
    client.table("contact_identities").insert({
        "contact_id": p_contact["id"],
        "source": "test",
        "source_external_id": "p_phone",
        "phone": "+15550009999",
    }).execute()

    try:
        # R1 is earlier (default imported_at); give both the SAME full_name so they
        # cluster together via name-sim ≥ REVIEW_BAND in clustering.py.
        r1 = _stage(db, full_name="Morgan Test", email="m@x.co",
                    source_external_id="R1")
        r2 = _stage(db, full_name="Morgan Test", email="m@x.co", phone="+15550009999",
                    source_external_id="R2")

        # Confirm they end up in the same cluster (shared email + name)
        v = _plan(db)
        assert v["R1"]["cluster_id"] == v["R2"]["cluster_id"], (
            "R1 and R2 must be in the same cluster for the member-vs-existing conflict"
        )

        # R1 has no existing hit (m@x.co not yet in DB) → creates a new contact
        assert v["R1"]["match_status"] == "merged", (
            f"R1 should create a new contact, got {v['R1']['match_status']}"
        )
        r1_create_key = v["R1"]["create_key"]

        # R2: email hits M (create_key from R1), phone hits P → conflicting_keys
        assert v["R2"]["match_status"] == "needs_review", (
            f"R2 should be needs_review, got {v['R2']['match_status']}"
        )
        assert v["R2"]["match_method"] == "conflicting_keys", (
            f"R2 match_method should be conflicting_keys, got {v['R2']['match_method']}"
        )
        # Field-order parity: email (→ M) comes before phone (→ P)
        # matched_ref MUST be M's create_key, NOT P's uuid
        assert v["R2"]["matched_ref"] == r1_create_key, (
            f"matched_ref should be M's create_key ({r1_create_key!r}), "
            f"got {v['R2']['matched_ref']!r} — field-order divergence from find_candidates"
        )
        assert v["R2"]["matched_ref"] != p_contact["id"], (
            "matched_ref must NOT be P (phone hit comes after email in field order)"
        )

    finally:
        # Clean up the live-DB contact created outside the test transaction
        client.table("contact_identities").delete().eq(
            "contact_id", p_contact["id"]).execute()
        client.table("contacts").delete().eq("id", p_contact["id"]).execute()
