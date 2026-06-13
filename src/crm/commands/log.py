"""Touchpoints. Facts are append-only; summaries are editable (spec §4).

Logging a touchpoint also refreshes the contact's denormalized last_touchpoint_*
fields when the new touchpoint is the most recent."""
import re
from datetime import date as date_t

import typer

from crm.commands.admin import require_agent
from crm.commands.contacts import _resolve
from crm.config import get_client
from crm.output import err

VALID_KINDS = {"origin", "event", "email", "message", "call", "meeting"}

event_app = typer.Typer(help="Shared occasions — group touchpoints linked to one event row.")


def _validate_iso_date(value: str | None) -> str | None:
    if value is None:
        return None
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        err(f"--date must be YYYY-MM-DD, got '{value}'")
        raise typer.Exit(1)
    return value


def _bump_last_touchpoint(client, contact_id: str, occurred: str | None,
                          channel: str | None, topic: str | None):
    if not occurred:
        return
    c = (client.table("contacts").select("last_touchpoint_at")
         .eq("id", contact_id).single().execute().data)
    if c["last_touchpoint_at"] and c["last_touchpoint_at"] >= occurred:
        return
    client.table("contacts").update(
        {"last_touchpoint_at": occurred, "last_touchpoint_channel": channel,
         "last_touchpoint_topic": topic, "updated_at": "now()"}
    ).eq("id", contact_id).execute()


def log(
    ref: str = typer.Argument(..., help="Contact name or uuid"),
    kind: str = typer.Option(..., "--kind",
                             help="origin|event|email|message|call|meeting"),
    channel: str = typer.Option(None, "--channel"),
    date: str = typer.Option(None, "--date", help="YYYY-MM-DD; default today"),
    summary: str = typer.Option(None, "--summary"),
    agent: str = typer.Option("rahul", "--agent"),
):
    """Log a 1:1 touchpoint."""
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
    agent: str = typer.Option("rahul", "--agent"),
):
    """One event row + one linked interaction per participant."""
    _validate_iso_date(date)
    client = get_client()
    require_agent(client, agent)

    # resolve every participant BEFORE inserting anything — a failure mid-loop
    # would otherwise leave a phantom half-built event with no cleanup path
    refs = [p.strip() for p in participants.split(",") if p.strip()]
    resolved = [_resolve(client, ref) for ref in refs]

    ev = client.table("events").insert(
        {"name": name, "occurred_at": date, "location": location,
         "event_notes": notes, "source": "manual", "created_by": agent}
    ).execute().data[0]
    count = 0
    for c in resolved:
        client.table("interactions").insert(
            {"contact_id": c["id"], "event_id": ev["id"], "kind": "event",
             "channel": "irl", "occurred_at": date, "logged_by": agent}
        ).execute()
        _bump_last_touchpoint(client, c["id"], date, "irl", name)  # topic = event name; per-person notes come later via event note
        count += 1
    err(f"event created with {count} participants")
    typer.echo(ev["id"])


@event_app.command("note")
def event_note(
    event_id: str = typer.Argument(...),
    ref: str = typer.Argument(..., help="Participant contact id/name"),
    text: str = typer.Argument(..., help="Per-person note within this event"),
    agent: str = typer.Option("rahul", "--agent"),
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
