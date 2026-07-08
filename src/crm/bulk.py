"""Shared helpers + constants for cohort-wide bulk operations (crm bulk *).

`CHUNK` bounds body-carried write batches (RPC `jsonb`/array payloads and
`.insert([...])` bodies — these travel in the request body, so 500 is safe).
`PAGE` bounds cohort-read pagination. Both are module constants so tests can
monkeypatch them small for fast boundary coverage; `PAGE` here is independent
of `backfill.PAGE`.
"""
import json
from datetime import date, timedelta

import typer

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


# ── gate + emit ──────────────────────────────────────────────────────────────

def _emit(affected_ids: list[str], cohort_count: int, dry_run: bool,
          as_json: bool, sample_names: list[dict] | None = None) -> None:
    """Print a result summary to stdout.

    JSON paths always emit a single JSON object.
    Human paths emit readable text — sample lines on dry-run, a tally on write.

    Args:
        affected_ids:  The ids that were (or would be) CHANGED. For set/log this
                       equals the cohort; for tag it's only the newly-tagged subset
                       (already-tagged contacts are excluded). `changed_count` is
                       reported as len(affected_ids) on the write path.
        cohort_count:  Total contacts that matched the filter (may be > len(
                       affected_ids), e.g. tag where some already had the tag).
        dry_run:       True → preview mode (no writes happened).
        as_json:       True → emit machine-readable JSON on stdout.
        sample_names:  List of {"id": ..., "full_name": ...} dicts for the human
                       dry-run preview (up to 10).  Unused in JSON mode or write mode.
    """
    if as_json:
        payload: dict = {
            "dry_run": dry_run,
            "cohort_count": cohort_count,
            "affected": affected_ids,
        }
        if not dry_run:
            payload["changed_count"] = len(affected_ids)
        typer.echo(json.dumps(payload))
    else:
        if dry_run:
            typer.echo(f"would affect {cohort_count} contacts:")
            for row in (sample_names or []):
                typer.echo(f"  {row['id']}  {row.get('full_name', '')}")
        else:
            typer.echo(f"{len(affected_ids)} changed ({cohort_count} matched)")


def _gate(client, *, status=None, tier=None, tag=None, affiliation=None,
          cold_since=None, all_: bool = False, dry_run: bool = False,
          yes: bool = False, as_json: bool = False,
          agent: str = "rahul") -> list[str] | None:
    """Resolve a cohort and enforce the --yes / --dry-run / --all write gate.

    Returns the list of ids to act on, or None as a STOP sentinel (caller must
    return immediately).  The sentinel is used for the dry-run preview path and
    for the empty-cohort path (nothing to do).

    Args:
        client:      Supabase client (or CountingClient spy in tests).
        status:      Filter by connection_status.
        tier:        Filter by closeness_tier.
        tag:         Filter contacts whose tags array contains this value.
        affiliation: Filter contacts whose affiliations array contains this value.
        cold_since:  Filter contacts cold for N months (last_touchpoint_at old/null).
        all_:        Bypass filter requirement — act on the entire contacts table.
        dry_run:     Preview without writing; skips --yes check and agent validation.
        yes:         Required for any write (non-dry-run) path.
        as_json:     Emit machine-readable JSON instead of human text.
        agent:       Agent id to validate via require_agent() on write paths.

    Raises:
        typer.Exit(2): Bad call — missing filter, bad filter + --all combo, or
                       missing --yes on a write path.
        typer.Exit(1): Agent not registered (propagated from require_agent).
    """
    from crm.commands.admin import require_agent
    from crm.output import err

    # 1. Need at least a filter or explicit --all.
    has_filter = any(v is not None and v is not False and v != ""
                     for v in (status, tier, tag, affiliation, cold_since))
    if not has_filter and not all_:
        err("refusing to act on all contacts; pass a filter or --all")
        raise typer.Exit(2)

    # 2. --all and a filter are mutually exclusive.
    if all_ and has_filter:
        err("--all cannot be combined with filters")
        raise typer.Exit(2)

    # 3. Write paths need --yes and a registered agent.
    if not dry_run:
        if not yes:
            err("pass --dry-run to preview or --yes to apply")
            raise typer.Exit(2)
        require_agent(client, agent)

    # 4. Resolve the cohort (when all_, pass every filter as None → full table).
    ids = _resolve_cohort(
        client,
        status=None if all_ else status,
        tier=None if all_ else tier,
        tag=None if all_ else tag,
        affiliation=None if all_ else affiliation,
        cold_since=None if all_ else cold_since,
    )

    # 5. Dry-run: fetch sample names and emit preview, then STOP.
    if dry_run:
        sample: list[dict] = []
        if ids:
            sample = (
                client.table("contacts")
                .select("id,full_name")
                .in_("id", ids[:10])
                .execute()
                .data
            )
        _emit(ids, len(ids), dry_run=True, as_json=as_json, sample_names=sample)
        return None

    # 6. Empty cohort on write path: nothing to do, emit zero tally, STOP.
    if not ids:
        _emit([], 0, dry_run=False, as_json=as_json)
        return None

    # 7. Happy path — hand the ids back to the caller to act on.
    return ids
