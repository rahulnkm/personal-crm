"""Touchpoints. Facts are append-only; summaries are editable (spec §4).

Logging a touchpoint also refreshes the contact's denormalized last_touchpoint_*
fields when the new touchpoint is the most recent."""
import re
from datetime import date as date_t

import typer

import crm.bulk as _bulk
from crm.commands.admin import require_agent
from crm.commands.contacts import _resolve
from crm.config import get_client
from crm.output import AGENT_HELP, err

VALID_KINDS = {"origin", "event", "email", "message", "call", "meeting"}

# UUID format: 8-4-4-4-12 hex chars separated by hyphens (36 chars total)
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
                      re.IGNORECASE)

event_app = typer.Typer(help="Shared occasions — group touchpoints linked to one event row.")


def _validate_iso_date(value: str | None) -> str | None:
    if value is None:
        return None
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        err(f"--date must be YYYY-MM-DD, got '{value}'")
        raise typer.Exit(1)
    return value


def _bump_last_touchpoint_bulk(client, ids: list[str], occurred: str | None,
                                channel: str | None, topic: str | None):
    """Server-side guarded monotonic bump for one or more contacts.

    The RPC runs a single UPDATE filtered by `< p_occurred`, so equal-date is a
    no-op and there is no read-before-write lost-update race. No-ops when
    `occurred` is None or `ids` is empty.
    """
    if not occurred or not ids:
        return
    for i in range(0, len(ids), _bulk.CHUNK):
        client.rpc("bulk_bump_last_touchpoint", {
            "p_ids": ids[i:i + _bulk.CHUNK],
            "p_occurred": occurred,
            "p_channel": channel,
            "p_topic": topic,
        }).execute()


def _bump_last_touchpoint(client, contact_id: str, occurred: str | None,
                          channel: str | None, topic: str | None):
    """Single-contact bump — delegates to the shared RPC-backed helper."""
    _bump_last_touchpoint_bulk(client, [contact_id], occurred, channel, topic)


def log(
    ref: str = typer.Argument(..., help="Contact name or uuid"),
    kind: str = typer.Option(..., "--kind",
                             help="origin|event|email|message|call|meeting"),
    channel: str = typer.Option(None, "--channel"),
    date: str = typer.Option(None, "--date", help="YYYY-MM-DD; default today"),
    summary: str = typer.Option(None, "--summary"),
    agent: str = typer.Option("rahul", "--agent", help=AGENT_HELP),
):
    """Log one dated touchpoint with a contact; refreshes their last-contact fields."""
    if kind not in VALID_KINDS:
        err(f"'{kind}' is not a valid kind. Valid: {sorted(VALID_KINDS)}")
        raise typer.Exit(1)
    _validate_iso_date(date)
    client = get_client()
    require_agent(client, agent)
    c = _resolve(client, ref)
    occurred = date or date_t.today().isoformat()
    client.table("interactions").insert(
        {"contact_id": c["id"], "kind": kind, "channel": channel,
         "occurred_at": occurred, "summary": summary, "logged_by": agent}
    ).execute()
    _bump_last_touchpoint(client, c["id"], occurred, channel, summary)
    typer.echo("logged")


@event_app.command("add")
def event_add(
    name: str = typer.Argument(...),
    date: str = typer.Option(None, "--date", help="YYYY-MM-DD"),
    location: str = typer.Option(None, "--location"),
    participants: str = typer.Option("", "--participants",
                                     help="Comma-separated contact ids/names"),
    notes: str = typer.Option(None, "--notes", help="Event-level notes"),
    agent: str = typer.Option("rahul", "--agent", help=AGENT_HELP),
):
    """One event row + one linked interaction per participant."""
    _validate_iso_date(date)
    client = get_client()
    require_agent(client, agent)

    # ── Resolve ALL participants before touching the DB ────────────────────────
    # Split refs into uuids vs names; batch-fetch uuids in one query.
    refs = [p.strip() for p in participants.split(",") if p.strip()]
    uuid_refs = [r for r in refs if _UUID_RE.match(r)]
    name_refs = [r for r in refs if not _UUID_RE.match(r)]

    resolved: list[dict] = []

    # Batch-resolve uuid refs in ONE query and fail fast on any missing.
    if uuid_refs:
        rows = (client.table("contacts").select("*")
                .in_("id", uuid_refs).execute().data)
        found_ids = {r["id"] for r in rows}
        missing = [u for u in uuid_refs if u not in found_ids]
        if missing:
            for m in missing:
                err(f"No contact with id '{m}'")
            raise typer.Exit(1)
        # Preserve original ref ordering for uuid portion
        by_id = {r["id"]: r for r in rows}
        resolved += [by_id[u] for u in uuid_refs]

    # Resolve name refs one-by-one (keeps fuzzy/ambiguity/exit-1 behaviour).
    for ref in name_refs:
        resolved.append(_resolve(client, ref))

    # ── All resolved — now write ───────────────────────────────────────────────
    ev = client.table("events").insert(
        {"name": name, "occurred_at": date, "location": location,
         "event_notes": notes, "source": "manual", "created_by": agent}
    ).execute().data[0]

    # One batched interactions insert (not one per participant).
    if resolved:
        client.table("interactions").insert([
            {"contact_id": c["id"], "event_id": ev["id"], "kind": "event",
             "channel": "irl", "occurred_at": date, "logged_by": agent}
            for c in resolved
        ]).execute()

    # One RPC-backed bulk bump (chunked; equal-date = no-op; no lost-update race).
    _bump_last_touchpoint_bulk(
        client, [c["id"] for c in resolved], date, "irl", name
    )

    err(f"event created with {len(resolved)} participants")
    typer.echo(ev["id"])


@event_app.command("note")
def event_note(
    event_id: str = typer.Argument(...),
    ref: str = typer.Argument(..., help="Participant contact id/name"),
    text: str = typer.Argument(..., help="Per-person note within this event"),
    agent: str = typer.Option("rahul", "--agent", help=AGENT_HELP),
):
    """Set/update the per-person summary on a participant's interaction row."""
    client = get_client()
    require_agent(client, agent)
    c = _resolve(client, ref)
    rows = (client.table("interactions").select("id,summary")
            .eq("event_id", event_id).eq("contact_id", c["id"]).execute().data)
    if not rows:
        err(f"{c['full_name']} is not a participant of event {event_id}")
        raise typer.Exit(1)
    if len(rows) > 1:
        err(f"Warning: {c['full_name']} has {len(rows)} interactions for this event; updating the first.")
    client.table("interactions").update(
        {"summary": text, "updated_at": "now()"}).eq("id", rows[0]["id"]).execute()
    typer.echo("noted")
