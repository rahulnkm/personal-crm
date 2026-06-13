from crm.matching import AUTO_MERGE, REVIEW_BAND, classify, find_candidates, CONFLICT_SCORE


def _mk_contact(db, name, email=None):
    c = db.table("contacts").insert({"full_name": name}).execute().data[0]
    db.table("contact_identities").insert(
        {"contact_id": c["id"], "source": "seed", "email": email}
    ).execute()
    return c


def test_exact_email_match(db):
    c = _mk_contact(db, "Ada Lovelace", email="ada@example.com")
    cand = find_candidates(db, {"full_name": "A. Lovelace", "email": "ada@example.com"})
    assert cand["method"] == "exact_email"
    assert cand["contact_id"] == c["id"]
    assert cand["score"] == 1.0


def test_fuzzy_name_match(db):
    c = _mk_contact(db, "Jonathan Smithers")
    cand = find_candidates(db, {"full_name": "Jonathon Smithers"})
    assert cand["method"] == "fuzzy_name"
    assert cand["contact_id"] == c["id"]
    assert cand["score"] >= REVIEW_BAND


def test_no_match_returns_none(db):
    _mk_contact(db, "Ada Lovelace")
    assert find_candidates(db, {"full_name": "Zebulon Quartz"}) is None


def test_classify_thresholds():
    assert classify(1.0) == "auto"
    assert classify(AUTO_MERGE) == "auto"
    assert classify((AUTO_MERGE + REVIEW_BAND) / 2) == "review"
    assert classify(REVIEW_BAND - 0.01) == "none"


def test_role_email_never_exact_matches(db):
    _mk_contact(db, "Alice Anderson", email="team@acme.com")
    cand = find_candidates(db, {"full_name": "Bob Baker", "email": "team@acme.com"})
    # role mailbox skipped as a key; names don't fuzzy-match → no candidate
    assert cand is None or cand["method"] != "exact_email"


def test_conflicting_keys_land_in_review_band(db):
    a = _mk_contact(db, "Alice Anderson", email="alice@a.co")
    b = db.table("contacts").insert({"full_name": "Bob Baker"}).execute().data[0]
    db.table("contact_identities").insert(
        {"contact_id": b["id"], "source": "seed", "linkedin_url": "linkedin.com/in/bob"}
    ).execute()
    cand = find_candidates(db, {"full_name": "X",
                                "email": "alice@a.co",
                                "linkedin_url": "linkedin.com/in/bob"})
    assert cand["method"] == "conflicting_keys"
    assert classify(cand["score"]) == "review"


def test_below_band_fuzzy_returns_none(db):
    _mk_contact(db, "Li Wu")
    # 'Li Wo' scores ~0.5 — RPC returns it (above the 0.3 prefilter) but it's
    # below REVIEW_BAND, exercising the drop branch
    assert find_candidates(db, {"full_name": "Li Wo"}) is None


def test_accents_no_longer_fragment(db):
    c = _mk_contact(db, "José García-Hernández")
    cand = find_candidates(db, {"full_name": "Jose Garcia-Hernandez"})
    assert cand is not None and cand["contact_id"] == c["id"]
    assert cand["score"] >= REVIEW_BAND
