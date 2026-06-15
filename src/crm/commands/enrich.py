"""`crm enrich` — the provenance-tracked write surface.

Every agent-discovered fact funnels through here:
  - apply: ingest a JSON payload of candidate facts (attribute → survivorship RPC;
    identifier → quarantine in candidate_identities).
  - review: human arbitration of the queue (approve/reject/skip).
  - changes: job-change detection over the provenance trail.
  - undo / forget / stats: reversal, redaction, coverage.
"""
import json
import sys

import typer

from crm.commands.admin import require_agent
from crm.commands.contacts import _resolve
from crm.config import get_client
from crm.enrich import ARRAY_FIELDS, ATTRIBUTE, IDENTIFIER, EnrichCandidate, parse_payload
from crm.output import err, render

enrich_app = typer.Typer(help="Provenance-tracked enrichment: apply, review, undo, stats.")


def _read_payload(file: str | None) -> str:
    if file:
        with open(file) as fh:
            return fh.read()
    return sys.stdin.read()


@enrich_app.command("apply")
def apply(
    ref: str = typer.Argument(..., help="Contact name or uuid"),
    file: str = typer.Option(None, "--file", help="JSON payload file (default: stdin)"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Report outcomes, write nothing"),
    agent: str = typer.Option("rahul", "--agent"),
    as_json: bool = typer.Option(False, "--json"),
):
    """Apply agent-discovered facts to a contact through the survivorship RPC."""
    client = get_client()
    require_agent(client, agent)
    raw = _read_payload(file)
    try:
        candidates = parse_payload(raw)
    except (ValueError, json.JSONDecodeError) as exc:
        err(f"bad payload: {exc}")
        raise typer.Exit(2)
    c = _resolve(client, ref)

    results = []
    for cand in candidates:
        if cand.kind == ATTRIBUTE:
            # array attributes (expertise/interests/tags/affiliations) accumulate via
            # the set-union RPC; scalar attributes go through survivorship.
            rpc = "enrich_apply_array" if cand.field in ARRAY_FIELDS else "enrich_apply_candidate"
            outcome = client.rpc(rpc, {
                "p_contact_id": c["id"], "p_field": cand.field, "p_value": cand.value,
                "p_method": _method_for(agent), "p_source": cand.source or agent,
                "p_confidence": cand.confidence, "p_source_detail": cand.source_detail,
                "p_dry_run": dry_run,
            }).execute().data
        else:  # IDENTIFIER
            outcome = _apply_identifier(client, c, cand, agent, dry_run)
        results.append({"field": cand.field, "outcome": outcome})

    if as_json:
        typer.echo(json.dumps(results, default=str))
    else:
        for r in results:
            typer.echo(f"{r['field']}: {r['outcome']}")


# closeness priority: t1 first … none last. in_network beats contact_on_file within a tier.
_TIER_RANK = {"t1_irl_messaging": 0, "t2_dm": 1, "t3_community": 2,
              "t4_public": 3, "none": 4}
# per-source minimum seconds between calls (spec §6.2 — throughput/safety, not cost).
_SOURCE_MIN_INTERVAL = {"gravatar": 60 / 50, "github": 60 / 10}
# fields a source produces that map to a contacts column (only-missing gap check).
# identifier fields (email/etc.) are never "missing-on-contacts", so we only check
# the attribute fields a source can fill.
_RUN_COLS = ("current_role", "current_company", "location", "company_category",
             "avatar_url", "github_username", "twitter_username", "website_url")


@enrich_app.command("run")
def run(
    sources: str = typer.Option(None, "--sources",
                                help="Comma list (default: all): gravatar,github"),
    status: str = typer.Option("in_network", "--status",
                               help="connection_status filter (default in_network)"),
    tier: str = typer.Option(None, "--tier", help="Comma list of closeness tiers"),
    limit: int = typer.Option(None, "--limit", help="Cap contacts processed (batching)"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Compute outcomes, write nothing"),
    only_missing: bool = typer.Option(
        True, "--only-missing/--no-only-missing",
        help="Skip contacts already enriched or with no gaps a source would fill"),
    as_json: bool = typer.Option(False, "--json"),
):
    """Fetch deterministic public signal (Gravatar/GitHub) and apply via the RPC.

    Walks the network closeness-first (t1 > t2 > … > none), runs each selected source
    on the contact's primary email, and funnels every Candidate through
    enrich_apply_candidate (method=enrich_api, source=<plugin>). Sets
    contacts.last_enriched_at on success (unless --dry-run). Per-contact status is
    always reported — never a silent skip.
    """
    from datetime import date

    from crm.sources import select_sources

    client = get_client()
    selected = select_sources(sources.split(",") if sources else None)
    if not selected:
        err(f"No known sources in {sources!r}; available: gravatar, github")
        raise typer.Exit(2)

    contacts = _candidate_contacts(client, status, tier)
    contacts.sort(key=lambda c: (
        _TIER_RANK.get(c.get("closeness_tier"), 99),
        0 if c.get("connection_status") == "in_network" else 1,
        c.get("full_name") or ""))

    limiter = _RateLimiter()
    results: list[dict] = []
    processed = 0
    today = date.today().isoformat()
    summary = {"contacts": 0, "enriched_contacts": 0, "enriched_fields": 0,
               "skipped": 0, "no_email": 0, "no_signal": 0, "errors": 0,
               "dry_run": dry_run}

    for c in contacts:
        if limit is not None and processed >= limit:
            break

        email = _primary_email(client, c["id"])
        produced_fields = set().union(*[s.produces for s in selected])
        if only_missing and _already_satisfied(c, produced_fields):
            results.append({"contact_id": c["id"], "name": c["full_name"],
                            "status": "skipped", "fields": 0})
            summary["skipped"] += 1
            continue
        if not email:
            results.append({"contact_id": c["id"], "name": c["full_name"],
                            "status": "no_email", "fields": 0})
            summary["no_email"] += 1
            continue

        processed += 1
        summary["contacts"] += 1
        fields_written = 0
        errored = False
        for src in selected:
            limiter.wait(src.name)
            try:
                cands = src.fetch(email)
            except Exception as exc:  # a source must never abort the contact
                err(f"{c['full_name']}: source {src.name} error: {exc}")
                errored = True
                continue
            for cand in cands:
                outcome = client.rpc("enrich_apply_candidate", {
                    "p_contact_id": c["id"], "p_field": cand.field,
                    "p_value": cand.value, "p_method": "enrich_api",
                    "p_source": src.name, "p_confidence": cand.confidence,
                    "p_source_detail": cand.source_detail, "p_dry_run": dry_run,
                }).execute().data
                if outcome == "golden":
                    fields_written += 1

        if not dry_run:
            client.table("contacts").update(
                {"last_enriched_at": today}).eq("id", c["id"]).execute()

        if errored and fields_written == 0:
            status_label = "error"
            summary["errors"] += 1
        elif fields_written:
            status_label = "enriched"
            summary["enriched_contacts"] += 1
            summary["enriched_fields"] += fields_written
        else:
            status_label = "no_signal"
            summary["no_signal"] += 1

        results.append({"contact_id": c["id"], "name": c["full_name"],
                        "status": status_label, "fields": fields_written})

    if as_json:
        typer.echo(json.dumps({"summary": summary, "contacts": results}, default=str))
    else:
        for r in results:
            typer.echo(f"{r['name']}: {r['status']}"
                       + (f" ({r['fields']} fields)" if r["fields"] else ""))
        typer.echo(
            f"— {summary['enriched_contacts']} enriched "
            f"({summary['enriched_fields']} fields), {summary['skipped']} skipped, "
            f"{summary['no_email']} no-email, {summary['no_signal']} no-signal, "
            f"{summary['errors']} errors"
            + (" [dry-run]" if dry_run else ""))


def _candidate_contacts(client, status: str | None, tier: str | None) -> list[dict]:
    cols = ("id,full_name,connection_status,closeness_tier,last_enriched_at,"
            + ",".join(_RUN_COLS))
    q = client.table("contacts").select(cols)
    if status:
        q = q.eq("connection_status", status)
    if tier:
        tiers = [t.strip() for t in tier.split(",") if t.strip()]
        if tiers:
            q = q.in_("closeness_tier", tiers)
    # page past the 1,000-row PostgREST cap
    rows: list[dict] = []
    start = 0
    while True:
        page = q.order("id").range(start, start + 999).execute().data
        rows.extend(page)
        if len(page) < 1000:
            break
        start += 1000
    return rows


def _primary_email(client, contact_id: str) -> str | None:
    """First non-null email across the contact's identities."""
    rows = (client.table("contact_identities").select("email")
            .eq("contact_id", contact_id).not_.is_("email", "null")
            .order("imported_at").execute().data)
    return rows[0]["email"] if rows else None


def _already_satisfied(contact: dict, produced_fields: set[str]) -> bool:
    """only-missing gate: skip if already enriched, or every attribute field the
    selected sources could fill is already populated on the golden record."""
    if contact.get("last_enriched_at"):
        return True
    gap_fields = [f for f in produced_fields if f in _RUN_COLS]
    if not gap_fields:
        return False
    return all(contact.get(f) for f in gap_fields)


class _RateLimiter:
    """Per-source minimum-interval gate (throughput/safety). Sleeps only as long as
    needed since the last call for that source."""

    def __init__(self):
        self._last: dict[str, float] = {}

    def wait(self, name: str) -> None:
        import time
        interval = _SOURCE_MIN_INTERVAL.get(name, 0)
        if interval <= 0:
            return
        now = time.monotonic()
        last = self._last.get(name)
        if last is not None:
            delay = interval - (now - last)
            if delay > 0:
                time.sleep(delay)
        self._last[name] = time.monotonic()


@enrich_app.command("review")
def review(
    approve: str = typer.Option(None, "--approve", help="Review id to accept"),
    reject: str = typer.Option(None, "--reject", help="Review id to reject (tombstone)"),
    skip: str = typer.Option(None, "--skip", help="Review id to skip"),
    approve_identity: str = typer.Option(
        None, "--approve-identity", help="candidate_identities id to promote to a live identity"),
    agent: str = typer.Option("rahul", "--agent"),
    as_json: bool = typer.Option(False, "--json"),
):
    """Arbitrate the enrichment review queue. Bare command lists open items."""
    client = get_client()
    actions = [a for a in (approve, reject, skip, approve_identity) if a]
    if len(actions) > 1:
        err("Pass only one of --approve / --reject / --skip / --approve-identity")
        raise typer.Exit(2)

    if approve_identity:
        require_agent(client, agent)
        _promote_identity(client, approve_identity, agent)
        return

    if not actions:  # list open items
        rows = (client.table("enrich_review")
                .select("id,contact_id,field,candidate_value,source,confidence,reason,"
                        "other_contact_id,created_at")
                .eq("status", "open").order("created_at").execute().data)
        render(rows, as_json)
        return

    require_agent(client, agent)
    review_id = actions[0]
    item = (client.table("enrich_review").select("*").eq("id", review_id).execute().data)
    if not item:
        err(f"No review item '{review_id}'")
        raise typer.Exit(1)
    item = item[0]

    if approve:
        client.rpc("enrich_apply_candidate", {
            "p_contact_id": item["contact_id"], "p_field": item["field"],
            "p_value": item["candidate_value"], "p_method": "manual_set",
            "p_source": agent, "p_confidence": 1.0,
            "p_source_detail": None, "p_dry_run": False}).execute()
        client.table("enrich_review").update(
            {"status": "resolved", "resolved_at": "now()"}).eq("id", review_id).execute()
        typer.echo(f"approved: {item['field']} = {item['candidate_value']}")
    elif reject:
        # tombstone the (field, value): a disputed provenance row this value can
        # never beat again. Array fields have no single winner, so route to the
        # array reject (writes disputed row + removes the element); scalars
        # tombstone then re-elect a surviving winner.
        if item["field"] in ARRAY_FIELDS:
            client.rpc("enrich_reject_array", {
                "p_contact_id": item["contact_id"], "p_field": item["field"],
                "p_value": item["candidate_value"]}).execute()
        else:
            client.table("enrichment_log").insert({
                "contact_id": item["contact_id"], "field": item["field"],
                "new_value": item["candidate_value"], "source": agent,
                "method": "enrich_reject", "verification_status": "disputed"}).execute()
            client.rpc("enrich_recompute_field", {
                "p_contact_id": item["contact_id"], "p_field": item["field"]}).execute()
        client.table("enrich_review").update(
            {"status": "resolved", "resolved_at": "now()"}).eq("id", review_id).execute()
        typer.echo(f"rejected (tombstoned): {item['field']} = {item['candidate_value']}")
    else:  # skip
        client.table("enrich_review").update(
            {"status": "skipped", "resolved_at": "now()"}).eq("id", review_id).execute()
        typer.echo(f"skipped: {review_id}")


def _promote_identity(client, candidate_id: str, agent: str) -> None:
    """Promote a quarantined identifier into a live contact_identities match key.

    Idempotent: the (source, source_external_id) unique index makes a re-promote
    a no-op rather than a duplicate.
    """
    import hashlib

    rows = client.table("candidate_identities").select("*").eq("id", candidate_id).execute().data
    if not rows:
        err(f"No candidate identity '{candidate_id}'")
        raise typer.Exit(1)
    ci = rows[0]
    # map the identifier kind onto the contact_identities column
    col = {"email": "email", "phone": "phone", "linkedin_url": "linkedin_url",
           "handle": "handle"}.get(ci["kind"])
    if col is None:
        err(f"Unknown identifier kind '{ci['kind']}'")
        raise typer.Exit(1)
    source = f"enrich:{ci['source']}"
    sxid = hashlib.sha256(ci["value"].encode()).hexdigest()
    # idempotent insert: the (source, source_external_id) unique index is partial
    # (source_external_id not null) so it can't drive ON CONFLICT inference —
    # guard with an existence check instead.
    exists = (client.table("contact_identities").select("id")
              .eq("source", source).eq("source_external_id", sxid).execute().data)
    if not exists:
        client.table("contact_identities").insert({
            "contact_id": ci["contact_id"], "source": source,
            "source_external_id": sxid, col: ci["value"],
        }).execute()
    client.table("candidate_identities").update(
        {"status": "promoted"}).eq("id", candidate_id).execute()
    typer.echo(f"promoted: {ci['kind']} = {ci['value']}")


@enrich_app.command("changes")
def changes(
    since: str = typer.Option(..., "--since", help="ISO date/timestamp lower bound"),
    as_json: bool = typer.Option(False, "--json"),
):
    """Job changes (company/role transitions) from the provenance trail since a date.

    Reads `enrichment_log` directly — the RPC already records old->new on every
    materialization, so there's no extra write path.
    """
    client = get_client()
    rows = (client.table("enrichment_log")
            .select("contact_id,field,old_value,new_value,created_at")
            .in_("field", ["current_company", "current_role"])
            .not_.is_("old_value", "null")
            .gte("created_at", since)
            .order("created_at", desc=True).execute().data)
    # keep only genuine transitions (old != new); 'None'-text guards legacy str(None)
    rows = [r for r in rows if r["old_value"] not in (None, "None")
            and r["old_value"] != r["new_value"]]
    out = [{"contact_id": r["contact_id"], "field": r["field"],
            "old": r["old_value"], "new": r["new_value"], "at": r["created_at"]}
           for r in rows]
    if as_json:
        typer.echo(json.dumps(out, default=str))
    else:
        render(out, False)


@enrich_app.command("undo")
def undo(
    ref: str = typer.Argument(..., help="Contact name or uuid"),
    field: str = typer.Argument(..., help="Scalar field to revert"),
    agent: str = typer.Option("rahul", "--agent"),
):
    """Revert the current (robot) value for a field and re-elect the prior winner.

    Tombstones the current value so it won't immediately re-win, then recomputes.
    """
    client = get_client()
    require_agent(client, agent)
    c = _resolve(client, ref)
    cur = (client.table("enrichment_log").select("id,new_value")
           .eq("contact_id", c["id"]).eq("field", field).eq("is_current", True)
           .execute().data)
    if not cur:
        err(f"No current value for {field} on {c['full_name']}")
        raise typer.Exit(1)
    # dispute the current value so recompute skips it and elects the next-best
    client.table("enrichment_log").update(
        {"verification_status": "disputed", "is_current": False}).eq("id", cur[0]["id"]).execute()
    client.rpc("enrich_recompute_field", {
        "p_contact_id": c["id"], "p_field": field}).execute()
    got = (client.table("contacts").select(field).eq("id", c["id"]).single().execute().data)
    typer.echo(f"{c['full_name']}: {field} reverted to {got.get(field)!r}")


@enrich_app.command("forget")
def forget(
    ref: str = typer.Argument(..., help="Contact name or uuid"),
    agent: str = typer.Option("rahul", "--agent"),
):
    """Redact (right-to-be-forgotten) enrichment values for a contact, keeping the
    structural provenance rows so the audit trail stays intact."""
    client = get_client()
    require_agent(client, agent)
    c = _resolve(client, ref)
    client.table("enrichment_log").update(
        {"old_value": None, "new_value": None, "redacted_at": "now()"}
    ).eq("contact_id", c["id"]).execute()
    typer.echo(f"redacted enrichment values for {c['full_name']}")


@enrich_app.command("stats")
def stats(as_json: bool = typer.Option(False, "--json")):
    """Enrichment coverage: current values by source, in-review queue, stale rows.
    Head-count queries so counts stay exact past PostgREST's 1,000-row cap."""
    client = get_client()
    from datetime import date

    def count_q(q) -> int:
        return q.execute().count or 0

    out = []
    # in-review (open queue)
    out.append({"metric": "in_review", "count": count_q(
        client.table("enrich_review").select("id", count="exact", head=True).eq("status", "open"))})
    # pending quarantined identifiers
    out.append({"metric": "pending_identities", "count": count_q(
        client.table("candidate_identities").select("id", count="exact", head=True).eq("status", "pending"))})
    # stale: current rows whose refresh_after is in the past
    out.append({"metric": "stale", "count": count_q(
        client.table("enrichment_log").select("id", count="exact", head=True)
        .eq("is_current", True).lt("refresh_after", date.today().isoformat()))})
    # current values grouped by source (one head-count per distinct source)
    cur_sources = (client.table("enrichment_log").select("source")
                   .eq("is_current", True).execute().data)
    by_source: dict[str, int] = {}
    for r in cur_sources:
        by_source[r["source"]] = by_source.get(r["source"], 0) + 1
    for src, n in sorted(by_source.items()):
        out.append({"metric": f"current_by_source={src}", "count": n})

    out = [o for o in out if o["count"]]
    render(out, as_json)


def _method_for(agent: str) -> str:
    """Agent-authored enrichment is a derived method (never manual_set)."""
    return "enrich_agent"


def _normalize_identifier(field: str, value: str | None) -> str | None:
    from crm import normalize
    fn = {
        "email": normalize.normalize_email,
        "linkedin_url": normalize.normalize_linkedin,
        "phone": normalize.normalize_phone,
    }.get(field)
    if fn is None:  # handle (or anything else) — keep verbatim, lowercased
        return value.strip().lower() if value else None
    return fn(value)


def _apply_identifier(client, contact: dict, cand: EnrichCandidate, agent: str,
                      dry_run: bool) -> str:
    """Route a discovered identifier through the no-duplicate-manufacturing branch.

    0 matches      → quarantine in candidate_identities (pending).
    1 match, self  → noop (we already know this identity).
    1 match, other → enrich_review (identifier_conflict) — never silently merge.
    >=2 matches    → conflicting keys → enrich_review (identifier_conflict).
    """
    from crm.matching import find_candidates

    norm = _normalize_identifier(cand.field, cand.value)
    if not norm:
        return "noop"

    # pass identifier-only dict (no full_name) to suppress the fuzzy-name fallback
    hit = find_candidates(client, {cand.field: norm})

    if hit is None:  # 0 matches
        if dry_run:
            return "quarantine"
        client.table("candidate_identities").upsert({
            "contact_id": contact["id"], "kind": cand.field, "value": norm,
            "source": cand.source or agent, "confidence": cand.confidence,
            "source_detail": cand.source_detail, "status": "pending",
        }, on_conflict="contact_id,kind,value").execute()
        return "quarantine"

    if hit["contact_id"] == contact["id"] and hit.get("score") == 1.0:
        return "noop"  # single exact hit on this very contact

    # exact hit on a different contact, or conflicting/ambiguous keys → human review
    if dry_run:
        return "review"
    other = hit["contact_id"] if hit["contact_id"] != contact["id"] else None
    client.table("enrich_review").insert({
        "contact_id": contact["id"], "field": cand.field, "candidate_value": norm,
        "source": cand.source or agent, "confidence": cand.confidence,
        "reason": "identifier_conflict", "other_contact_id": other,
    }).execute()
    return "review"
