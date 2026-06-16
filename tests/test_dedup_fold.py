"""Tests for the batched auto_matched fold (Finding 1: kill the dedup N+1).

Two layers:
  - PURE function tests for `_fold_auto` — no DB. Pin the serial-replay semantics
    (cross-item fill, accumulator write-back, identity-conflict routing) in memory.
  - DB-backed `_execute_cluster`-in-ISOLATION tests — assert outcome patches and the
    N-invariance round-trip contract via tests/_spy.py CountingClient. We call
    `_execute_cluster` directly (NOT the `dedup` command) so build_plan/require_agent
    don't pollute the DB-call counts.
"""
import threading

from crm.commands.dedup import IDENTITY_FIELDS, FILL, _execute_cluster, _fold_auto
from crm.dedup_plan import build_plan
from tests._spy import CountingClient


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _ident(source="s", sxid=None, **kw):
    base = {f: None for f in IDENTITY_FIELDS}
    base["source"] = source
    base["source_external_id"] = sxid
    base.update(kw)
    return base


def _item(iid, cid, *, source="s", sxid=None, staged=None, ident=None,
          match_confidence=1.0, match_method="exact_email"):
    return {
        "id": iid,
        "source": source,
        "source_external_id": sxid,
        "cluster_id": "c1",
        "match_status": "auto_matched",
        "match_method": match_method,
        "match_confidence": match_confidence,
        "matched_ref": cid,
        "identity": ident if ident is not None else _ident(source, sxid),
        "staged": staged or {},
    }


def _identity(deref):
    return deref  # readability alias


# --------------------------------------------------------------------------- #
# PURE fold tests (no DB)
# --------------------------------------------------------------------------- #
def test_fold_attached_vs_conflict_outcomes():
    # one attachable item, one whose identity points elsewhere -> conflict
    it_ok = _item("rowA", "C1", sxid="e1", ident=_ident("s", "e1", email="a@x.co"),
                  staged={"company": "Acme"}, match_confidence=0.97)
    it_conf = _item("rowB", "C1", sxid="e2", ident=_ident("s", "e2", email="b@x.co"),
                    staged={"company": "Other"})
    contact_by_id = {"C1": {"id": "C1", "full_name": "Ada", "current_company": None,
                            "current_role": None, "location": None}}
    existing = {("s", "e2"): "C_OTHER"}  # e2 already on a DIFFERENT contact
    deref = lambda r: r

    id_inserts, enrich_rows, fills, outcomes = _fold_auto(
        [it_ok, it_conf], deref, contact_by_id, existing)

    assert outcomes["rowA"] == "attached"
    assert outcomes["rowB"] == "conflict"
    # conflict item produced NO fill / NO identity insert
    assert all(r["contact_id"] != "C1" or r["field"] == "current_company"
               for r in enrich_rows)


def test_fold_cross_item_fill_earlier_wins_later_logs_against_accumulator():
    """The load-bearing serial-semantics guard: two auto items, same contact, both
    fill the SAME null column with DIFFERENT values. Earlier (plan order) wins the
    fill; later logs import_conflict with old_value == EARLIER item's value (the
    in-memory accumulator), NOT the pre-cluster DB null."""
    it1 = _item("r1", "C1", sxid="e1", ident=_ident("s", "e1"),
                staged={"company": "FirstCo"}, source="srcA")
    it2 = _item("r2", "C1", sxid="e2", ident=_ident("s", "e2"),
                staged={"company": "SecondCo"}, source="srcB")
    contact_by_id = {"C1": {"id": "C1", "full_name": "Ada", "current_company": None,
                            "current_role": None, "location": None}}
    deref = lambda r: r

    id_inserts, enrich_rows, fills, outcomes = _fold_auto(
        [it1, it2], deref, contact_by_id, existing={})

    assert fills["C1"]["current_company"] == "FirstCo"   # earlier wins
    conflicts = [r for r in enrich_rows if r["field"] == "current_company"]
    assert len(conflicts) == 1
    c = conflicts[0]
    assert c["old_value"] == "FirstCo"     # accumulator value, not pre-cluster null
    assert c["new_value"] == "SecondCo"
    assert c["method"] == "import_conflict"
    assert c["source"] == "srcB"           # later item's source


def test_fold_same_contact_existing_identity_no_reinsert_still_fills():
    it = _item("r1", "C1", sxid="e1", ident=_ident("s", "e1", email="a@x.co"),
               staged={"company": "Acme"})
    contact_by_id = {"C1": {"id": "C1", "full_name": "Ada", "current_company": None,
                            "current_role": None, "location": None}}
    existing = {("s", "e1"): "C1"}  # identity already on the SAME contact
    deref = lambda r: r

    id_inserts, enrich_rows, fills, outcomes = _fold_auto(
        [it], deref, contact_by_id, existing)

    assert id_inserts == []                          # no re-insert
    assert outcomes["r1"] == "attached"
    assert fills["C1"]["current_company"] == "Acme"  # fill still runs


def test_fold_identity_conflict_routes_to_conflict_no_fill():
    it = _item("r1", "C1", sxid="e1", ident=_ident("s", "e1"),
               staged={"company": "Acme"})
    contact_by_id = {"C1": {"id": "C1", "full_name": "Ada", "current_company": None,
                            "current_role": None, "location": None}}
    existing = {("s", "e1"): "C_OTHER"}  # identity on a DIFFERENT contact
    deref = lambda r: r

    id_inserts, enrich_rows, fills, outcomes = _fold_auto(
        [it], deref, contact_by_id, existing)

    assert outcomes["r1"] == "conflict"
    assert id_inserts == []
    assert fills == {}
    assert enrich_rows == []


def test_fold_full_name_conflict_logged():
    it = _item("r1", "C1", sxid="e1", ident=_ident("s", "e1"),
               staged={"full_name": "Ada K. Lovelace"})
    contact_by_id = {"C1": {"id": "C1", "full_name": "Ada Lovelace",
                            "current_company": None, "current_role": None,
                            "location": None}}
    deref = lambda r: r

    id_inserts, enrich_rows, fills, outcomes = _fold_auto(
        [it], deref, contact_by_id, existing={})

    name_logs = [r for r in enrich_rows if r["field"] == "full_name"]
    assert len(name_logs) == 1
    assert name_logs[0]["old_value"] == "Ada Lovelace"
    assert name_logs[0]["new_value"] == "Ada K. Lovelace"
    assert name_logs[0]["method"] == "import_conflict"


def test_fold_deref_create_key_before_contact_lookup():
    """matched_ref may be a create_key; deref maps it to the real uuid BEFORE we
    look the contact up in the accumulator."""
    it = _item("r1", "ck:1", sxid="e1", ident=_ident("s", "e1"),
               staged={"company": "Acme"})
    contact_by_id = {"REAL": {"id": "REAL", "full_name": "Ada", "current_company": None,
                              "current_role": None, "location": None}}
    deref = lambda r: "REAL" if r == "ck:1" else r

    id_inserts, enrich_rows, fills, outcomes = _fold_auto(
        [it], deref, contact_by_id, existing={})

    assert outcomes["r1"] == "attached"
    assert fills["REAL"]["current_company"] == "Acme"
    assert id_inserts[0]["contact_id"] == "REAL"


# --------------------------------------------------------------------------- #
# DB-backed: _execute_cluster in ISOLATION
# --------------------------------------------------------------------------- #
def _run_cluster(db, items):
    """Run a single cluster's items through _execute_cluster with a fresh state/lock,
    wrapped in a CountingClient. Returns the spy for round-trip assertions."""
    spy = CountingClient(db)
    state = {"created": 0, "auto": 0, "review": 0, "rejected": 0, "errors": []}
    lock = threading.Lock()
    _execute_cluster(spy, items, state, lock)
    return spy, state


def _plan_one_cluster(db):
    """Build the plan from current pending staging rows and return the single cluster."""
    pending = (db.table("staging").select("*").eq("match_status", "pending")
               .order("imported_at").execute().data)
    plan = build_plan(db, pending)
    by_cluster = {}
    for p in plan:
        by_cluster.setdefault(p["cluster_id"], []).append(p)
    assert len(by_cluster) == 1, f"expected 1 cluster, got {len(by_cluster)}"
    return next(iter(by_cluster.values()))


def test_execute_attached_patch_carries_confidence_no_resolved_absence(db):
    c = db.table("contacts").insert({"full_name": "Ada Lovelace"}).execute().data[0]
    db.table("contact_identities").insert(
        {"contact_id": c["id"], "source": "s1", "source_external_id": "x1",
         "email": "ada@x.co"}).execute()
    db.table("staging").insert(
        {"source": "s2", "source_external_id": "x2", "full_name": "Ada Lovelace",
         "email": "ada@x.co", "company": "Analytical", "match_status": "pending"}).execute()
    items = _plan_one_cluster(db)
    _run_cluster(db, items)
    staged = (db.table("staging").select("*").eq("source_external_id", "x2")
              .single().execute().data)
    assert staged["match_status"] == "auto_matched"
    assert staged["match_confidence"] is not None
    assert staged["resolved_at"] is not None
    assert db.table("contacts").select("current_company").eq("id", c["id"]).single()\
        .execute().data["current_company"] == "Analytical"


def test_execute_identity_conflict_routes_needs_review_rerun_conflict(db):
    # two existing contacts; staging row's identity already on contact A (different
    # from the fuzzy/cluster target) -> rerun_conflict -> needs_review.
    a = db.table("contacts").insert({"full_name": "Ada Lovelace"}).execute().data[0]
    b = db.table("contacts").insert({"full_name": "Ada Lovelace"}).execute().data[0]
    # identity 'x1/email' belongs to A
    db.table("contact_identities").insert(
        {"contact_id": a["id"], "source": "s1", "source_external_id": "x1",
         "email": "ada@x.co"}).execute()
    # seed B with a SECOND identity so the staging row exact-keys to B, but its OWN
    # (source, sxid) already lives on A -> the rerun_conflict path.
    db.table("contact_identities").insert(
        {"contact_id": b["id"], "source": "s1", "source_external_id": "phoneB",
         "phone": "+15550009999"}).execute()
    db.table("staging").insert(
        {"source": "s1", "source_external_id": "x1", "full_name": "Ada Lovelace",
         "email": "irrelevant@x.co", "phone": "+15550009999",
         "match_status": "pending"}).execute()
    items = _plan_one_cluster(db)
    _run_cluster(db, items)
    staged = (db.table("staging").select("match_status,match_method")
              .eq("source_external_id", "x1").single().execute().data)
    assert staged["match_status"] == "needs_review"
    assert staged["match_method"] == "rerun_conflict"


def test_execute_duplicate_keys_two_rows_one_cluster_no_abort(db):
    # two DISTINCT staging row ids that share the SAME (source, source_external_id)
    # cannot exist (unique constraint). Instead: two rows with DIFFERENT sxid but
    # the SAME email -> both exact-match the same contact in one cluster. The batched
    # identity insert must not abort even though one identity may pre-exist.
    c = db.table("contacts").insert({"full_name": "Ada"}).execute().data[0]
    db.table("contact_identities").insert(
        {"contact_id": c["id"], "source": "s", "source_external_id": "seed",
         "email": "a@b.co"}).execute()
    db.table("staging").insert([
        {"source": "s", "source_external_id": "r1", "full_name": "Ada", "email": "a@b.co",
         "company": "Acme", "match_status": "pending", "imported_at": "2020-01-01T00:00:01Z"},
        {"source": "s", "source_external_id": "r2", "full_name": "Ada", "email": "a@b.co",
         "location": "NYC", "match_status": "pending", "imported_at": "2020-01-01T00:00:02Z"},
    ]).execute()
    items = _plan_one_cluster(db)
    _run_cluster(db, items)
    # both rows resolved & re-associated to the correct staging ids
    rows = {r["source_external_id"]: r["match_status"]
            for r in db.table("staging").select("source_external_id,match_status")
            .in_("source_external_id", ["r1", "r2"]).execute().data}
    assert rows == {"r1": "auto_matched", "r2": "auto_matched"}
    fresh = db.table("contacts").select("current_company,location").eq("id", c["id"])\
        .single().execute().data
    assert fresh["current_company"] == "Acme" and fresh["location"] == "NYC"
    # only 1 contact total -> no dup created
    assert len(db.table("contacts").select("id").execute().data) == 1


def _stage_k_auto(db, k):
    """Seed one existing contact + K staging rows that all auto-match it (distinct
    emails, all already-known on the SAME contact so they attach, distinct sxids)."""
    c = db.table("contacts").insert({"full_name": "Ada"}).execute().data[0]
    db.table("contact_identities").insert(
        {"contact_id": c["id"], "source": "s", "source_external_id": "seed",
         "email": "anchor@b.co"}).execute()
    rows = []
    for i in range(k):
        # all bring the SAME company -> first fills the null, rest are accumulator
        # no-ops. So fills collapse to ONE contacts.update regardless of K.
        rows.append({"source": "s", "source_external_id": f"r{i}", "full_name": "Ada",
                     "email": "anchor@b.co", "company": "Acme", "match_status": "pending",
                     "imported_at": f"2020-01-01T00:00:{i:02d}Z"})
    db.table("staging").insert(rows).execute()
    return c


def test_execute_n_invariance_k2_vs_k8(db):
    """K=2 vs K=8 auto-matched items on ONE contact issue the SAME number of DB
    calls; contacts.update == distinct contacts; identities insert <= 1;
    enrichment_log insert <= 1. Not a brittle exact total — only the K-invariance."""
    _stage_k_auto(db, 2)
    spy2, _ = _run_cluster(db, _plan_one_cluster(db))

    # clean slate for the K=8 run
    for t in ("enrichment_log", "staging", "contact_identities", "contacts"):
        db.table(t).delete().neq("id", "00000000-0000-0000-0000-000000000000").execute()

    _stage_k_auto(db, 8)
    spy8, _ = _run_cluster(db, _plan_one_cluster(db))

    assert spy2.total() == spy8.total(), \
        f"K-variance leak: K2={spy2.calls} vs K8={spy8.calls}"
    assert spy8.count("contacts", "update") == 1          # one distinct contact
    assert spy8.rpc_count("bulk_insert_identities") <= 1  # batched identity write
    assert spy8.count("enrichment_log", "insert") <= 1
