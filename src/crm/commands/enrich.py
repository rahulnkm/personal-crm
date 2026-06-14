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
from crm.enrich import ATTRIBUTE, IDENTIFIER, EnrichCandidate, parse_payload
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
            outcome = client.rpc("enrich_apply_candidate", {
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
        # never beat again, then re-elect a surviving winner.
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
