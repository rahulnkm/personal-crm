import uuid


def test_enrichment_substrate_schema(db):
    # new enrichment_log columns
    row = db.table("enrichment_log").select(
        "source_detail, verification_status, refresh_after, is_current").limit(1).execute()
    assert row is not None
    # new contacts columns
    c = db.table("contacts").select(
        "company_category, company_description, company_domain, expertise, interests, "
        "avatar_url, github_username, twitter_username, website_url").limit(1).execute()
    assert c is not None
    # new tables exist
    assert db.table("enrich_review").select("id").limit(1).execute() is not None
    assert db.table("candidate_identities").select("id").limit(1).execute() is not None


def _contact(db, **kw):
    base = {"full_name": "Test Person"}; base.update(kw)
    return db.table("contacts").insert(base).execute().data[0]


def _apply(db, cid, field, value, method, source, conf, dry=False):
    return db.rpc("enrich_apply_candidate", {
        "p_contact_id": cid, "p_field": field, "p_value": value,
        "p_method": method, "p_source": source, "p_confidence": conf,
        "p_source_detail": None, "p_dry_run": dry}).execute().data


def test_fills_null_field_becomes_golden(db):
    c = _contact(db, current_company=None)
    out = _apply(db, c["id"], "current_company", "Acme", "enrich_api", "gravatar", 0.9)
    assert out == "golden"
    got = db.table("contacts").select("current_company").eq("id", c["id"]).single().execute().data
    assert got["current_company"] == "Acme"


def test_manual_never_clobbered(db):
    c = _contact(db)
    _apply(db, c["id"], "current_company", "RealCo", "manual_set", "rahul", 1.0)
    out = _apply(db, c["id"], "current_company", "BrokerCo", "enrich_api", "pdl", 0.95)
    assert out in ("review", "losing")
    got = db.table("contacts").select("current_company").eq("id", c["id"]).single().execute().data
    assert got["current_company"] == "RealCo"  # manual stands


def test_low_confidence_goes_to_review_not_golden(db):
    c = _contact(db, current_role=None)
    out = _apply(db, c["id"], "current_role", "Wizard", "enrich_agent", "agent:claude-web", 0.5)
    assert out == "review"
    assert db.table("contacts").select("current_role").eq("id", c["id"]).single().execute().data["current_role"] is None
    assert len(db.table("enrich_review").select("id").eq("contact_id", c["id"]).execute().data) == 1


def test_dry_run_mutates_nothing(db):
    c = _contact(db, location=None)
    out = _apply(db, c["id"], "location", "SF", "enrich_api", "gravatar", 0.9, dry=True)
    assert out == "golden"  # would-be outcome
    assert db.table("contacts").select("location").eq("id", c["id"]).single().execute().data["location"] is None


def test_idempotent_reapply(db):
    c = _contact(db, location=None)
    _apply(db, c["id"], "location", "NYC", "enrich_api", "gravatar", 0.9)
    _apply(db, c["id"], "location", "NYC", "enrich_api", "gravatar", 0.9)
    rows = db.table("enrichment_log").select("id").eq("contact_id", c["id"]).eq("field","location").execute().data
    assert len(rows) == 1


def test_exactly_one_current(db):
    c = _contact(db, location=None)
    _apply(db, c["id"], "location", "NYC", "enrich_api", "gravatar", 0.9)
    _apply(db, c["id"], "location", "LA", "enrich_api", "pdl", 0.95)  # newer+higher → new winner
    cur = db.table("enrichment_log").select("new_value").eq("contact_id", c["id"]).eq("field","location").eq("is_current", True).execute().data
    assert len(cur) == 1
