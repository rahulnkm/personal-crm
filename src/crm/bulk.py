"""Shared helpers + constants for cohort-wide bulk operations (crm bulk *).

`CHUNK` bounds write batches (per-statement payloads / `.in_()` URL length) and
`PAGE` bounds cohort-read pagination. Both are module constants so tests can
monkeypatch them to small values for fast boundary coverage. `PAGE` here is
independent of `backfill.PAGE`.
"""
from datetime import date, timedelta

CHUNK = 500
PAGE = 1000


def _apply_filters(q, *, status=None, tier=None, tag=None,
                   affiliation=None, cold_since=None):
    """Apply the five list-filter clauses to query builder *q* and return it.

    This is a pure filter helper — it never calls .select(), .order(),
    .limit(), or .range() so it composes safely with both list_contacts
    (which needs ordering + a limit) and _resolve_cohort (which needs
    range-based pagination over the full set).
    """
    if status:
        q = q.eq("connection_status", status)
    if tier:
        q = q.eq("closeness_tier", tier)
    if tag:
        q = q.contains("tags", [tag])
    if affiliation:
        q = q.contains("affiliations", [affiliation])
    if cold_since is not None:
        cutoff = (date.today() - timedelta(days=30 * cold_since)).isoformat()
        q = q.or_(f"last_touchpoint_at.lte.{cutoff},last_touchpoint_at.is.null")
    return q


def _resolve_cohort(client, *, status=None, tier=None, tag=None,
                    affiliation=None, cold_since=None) -> list[str]:
    """Return sorted distinct contact ids matching the given filters.

    Paginates through the table in PAGE-sized windows so it works correctly
    for cohorts larger than PostgREST's single-response cap.  Stops as soon
    as a page comes back shorter than PAGE (the short-page exit condition).
    """
    ids: list[str] = []
    i = 0
    while True:
        q = client.table("contacts").select("id")
        q = _apply_filters(q, status=status, tier=tier, tag=tag,
                           affiliation=affiliation, cold_since=cold_since)
        page = q.range(i, i + PAGE - 1).execute().data
        ids.extend(r["id"] for r in page)
        if len(page) < PAGE:
            break
        i += PAGE
    return sorted(set(ids))
