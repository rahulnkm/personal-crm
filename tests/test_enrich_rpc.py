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
