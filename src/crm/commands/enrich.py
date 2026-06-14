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


def _method_for(agent: str) -> str:
    """Agent-authored enrichment is a derived method (never manual_set)."""
    return "enrich_agent"


def _apply_identifier(client, contact: dict, cand: EnrichCandidate, agent: str,
                      dry_run: bool) -> str:
    """Identifier routing → candidate_identities. Filled in Task 9."""
    raise NotImplementedError("identifier routing lands in Task 9")
