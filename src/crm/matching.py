"""Entity resolution: deterministic match keys first, pg_trgm fuzzy second.

Two thresholds (the gap between them IS the clerical-review queue):
  score >= AUTO_MERGE          -> attach automatically
  REVIEW_BAND <= score < AUTO  -> staging.needs_review, Rahul arbitrates
  score < REVIEW_BAND          -> new contact
Deterministic hits score 1.0 by definition.
"""
from supabase import Client

AUTO_MERGE = 0.92   # name-only fuzzy auto-merge is risky; keep this high
REVIEW_BAND = 0.55

# shared mailboxes are not a person — never let them be a 1.0 match key
ROLE_LOCALPARTS = frozenset({
    "info", "team", "hello", "contact", "admin", "support",
    "office", "sales", "hi", "events", "press", "careers", "jobs",
})

CONFLICT_SCORE = 0.75  # inside the review band by construction


def classify(score: float) -> str:
    if score >= AUTO_MERGE:
        return "auto"
    if score >= REVIEW_BAND:
        return "review"
    return "none"


def _is_role_email(email: str) -> bool:
    return email.split("@", 1)[0] in ROLE_LOCALPARTS


def find_candidates(client: Client, identity: dict) -> dict | None:
    """Return best candidate {contact_id, score, method} or None.

    identity fields are already normalized (importer's job).
    Deterministic keys score 1.0 — but if different keys point at DIFFERENT
    contacts (conflicting hard evidence), that's exactly what clerical review
    exists for: return CONFLICT_SCORE so it lands in the review band.
    """
    hits: dict[str, str] = {}  # contact_id -> first method that found it
    for field, method in (("email", "exact_email"),
                          ("linkedin_url", "exact_linkedin"),
                          ("phone", "exact_phone")):
        value = identity.get(field)
        if not value:
            continue
        if field == "email" and _is_role_email(value):
            continue
        rows = (client.table("contact_identities")
                .select("contact_id").eq(field, value).limit(2).execute().data)
        for r in rows:
            hits.setdefault(r["contact_id"], method)
    if len(hits) == 1:
        cid, method = next(iter(hits.items()))
        return {"contact_id": cid, "score": 1.0, "method": method}
    if len(hits) > 1:
        cid, method = next(iter(hits.items()))
        return {"contact_id": cid, "score": CONFLICT_SCORE, "method": "conflicting_keys"}

    name = identity.get("full_name")
    if not name:
        return None
    rows = client.rpc("match_contacts_by_name", {"q": name, "lim": 1}).execute().data
    if not rows:
        return None
    best = rows[0]
    if best["score"] < REVIEW_BAND:
        return None
    return {"contact_id": best["contact_id"], "score": best["score"],
            "method": "fuzzy_name"}
