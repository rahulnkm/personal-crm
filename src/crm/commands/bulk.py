"""Cohort-wide write commands (crm bulk *).

Each verb validates its own assignment, then hands cohort resolution + the
--yes/--dry-run/--all write-gate to crm.bulk._gate. The gate returns the list
of ids to act on (or None as a STOP sentinel — it has already emitted the
dry-run preview / empty-cohort tally). On the happy path the gate does NOT emit;
the verb writes per CHUNK-sized slice and then emits the final tally itself.
"""
from datetime import date as date_t

import typer

from crm.bulk import CHUNK, URL_CHUNK, _emit, _gate
from crm.commands.contacts import ARRAY_FIELDS, ENUM_VALUES, SETTABLE
from crm.commands.log import VALID_KINDS, _bump_last_touchpoint_bulk, _validate_iso_date
from crm.config import get_client
from crm.output import err

bulk_app = typer.Typer(help="Cohort-wide writes. Filter to a cohort, then apply.")


@bulk_app.command("set")
def bulk_set(
    assignment: str = typer.Argument(..., help="field=value (scalar fields only)"),
    status: str = typer.Option(None, "--status"),
    tier: str = typer.Option(None, "--tier"),
    tag: str = typer.Option(None, "--tag"),
    affiliation: str = typer.Option(None, "--affiliation"),
    cold_since: int = typer.Option(None, "--cold-since",
                                   help="Months since last touchpoint (or never)"),
    all_: bool = typer.Option(False, "--all", help="Act on every contact (no filter)"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview without writing"),
    yes: bool = typer.Option(False, "--yes", help="Required to apply a write"),
    as_json: bool = typer.Option(False, "--json"),
    agent: str = typer.Option("rahul", "--agent"),
):
    """Set a scalar field on every contact in the cohort. For tags use crm bulk tag."""
    if "=" not in assignment:
        err("Expected field=value")
        raise typer.Exit(2)
    field, value = assignment.split("=", 1)
    if field not in SETTABLE:
        err(f"'{field}' is not settable. Settable: {sorted(SETTABLE)}")
        raise typer.Exit(1)
    if field in ARRAY_FIELDS:
        err("bulk set handles scalar fields; for tags use: crm bulk tag <tag>")
        raise typer.Exit(2)
    if field in ENUM_VALUES and value not in ENUM_VALUES[field]:
        err(f"'{value}' is not a valid {field}. Valid: {sorted(ENUM_VALUES[field])}")
        raise typer.Exit(1)

    client = get_client()
    ids = _gate(client, status=status, tier=tier, tag=tag, affiliation=affiliation,
                cold_since=cold_since, all_=all_, dry_run=dry_run, yes=yes,
                as_json=as_json, agent=agent)
    if ids is None:  # gate already emitted (dry-run preview / empty cohort) or raised
        return

    # bulk_set's read + update use .in_("id", ...) which travels in the URL, so
    # chunk by URL_CHUNK (not CHUNK) to stay under the URL-length ceiling.
    for i in range(0, len(ids), URL_CHUNK):
        chunk = ids[i:i + URL_CHUNK]
        # capture pre-write values so the audit log records the real old_value
        before = (client.table("contacts").select("id," + field)
                  .in_("id", chunk).execute().data)
        old = {r["id"]: r.get(field) for r in before}
        (client.table("contacts")
         .update({field: value, "updated_at": "now()"})
         .in_("id", chunk).execute())
        client.table("enrichment_log").insert(
            [{"contact_id": cid, "field": field, "old_value": str(old.get(cid)),
              "new_value": str(value), "source": agent, "method": "bulk_set"}
             for cid in chunk]
        ).execute()

    # changed == cohort for set (every matched row is written)
    _emit(ids, len(ids), dry_run=False, as_json=as_json)


@bulk_app.command("tag")
def bulk_tag(
    tag: str = typer.Argument(..., help="Tag to add (must already be in tag_registry)"),
    status: str = typer.Option(None, "--status"),
    tier: str = typer.Option(None, "--tier"),
    tag_filter: str = typer.Option(None, "--tag", help="Filter cohort by existing tag"),
    affiliation: str = typer.Option(None, "--affiliation"),
    cold_since: int = typer.Option(None, "--cold-since",
                                   help="Months since last touchpoint (or never)"),
    all_: bool = typer.Option(False, "--all", help="Act on every contact (no filter)"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview without writing"),
    yes: bool = typer.Option(False, "--yes", help="Required to apply a write"),
    as_json: bool = typer.Option(False, "--json"),
    agent: str = typer.Option("rahul", "--agent"),
):
    """Add a tag to every contact in the cohort. Tag must exist in the registry."""
    client = get_client()

    # Registry check: fail fast with a clear message before gate/cohort resolution.
    rows = client.table("tag_registry").select("tag").eq("tag", tag).execute().data
    if not rows:
        err(f"Tag '{tag}' not in registry. First: crm tags add {tag} --desc '...'")
        raise typer.Exit(1)

    ids = _gate(client, status=status, tier=tier, tag=tag_filter,
                affiliation=affiliation, cold_since=cold_since,
                all_=all_, dry_run=dry_run, yes=yes, as_json=as_json, agent=agent)
    if ids is None:  # gate already emitted (dry-run preview / empty cohort) or raised
        return

    affected: list[str] = []
    for i in range(0, len(ids), CHUNK):
        chunk = ids[i:i + CHUNK]
        result = client.rpc("bulk_add_tag", {"p_tag": tag, "p_ids": chunk}).execute().data
        for r in (result or []):
            affected.append(r["bulk_add_tag"] if isinstance(r, dict) else r)

    # cohort_count = len(ids) (all matched); changed_count = len(affected) (newly tagged)
    _emit(affected, len(ids), dry_run=False, as_json=as_json)


@bulk_app.command("log")
def bulk_log(
    kind: str = typer.Option(..., "--kind",
                             help="origin|event|email|message|call|meeting"),
    channel: str = typer.Option(None, "--channel"),
    date: str = typer.Option(None, "--date", help="YYYY-MM-DD; default today"),
    summary: str = typer.Option(None, "--summary"),
    status: str = typer.Option(None, "--status"),
    tier: str = typer.Option(None, "--tier"),
    tag: str = typer.Option(None, "--tag", help="Filter cohort by tag"),
    affiliation: str = typer.Option(None, "--affiliation"),
    cold_since: int = typer.Option(None, "--cold-since",
                                   help="Months since last touchpoint (or never)"),
    all_: bool = typer.Option(False, "--all", help="Act on every contact (no filter)"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview without writing"),
    yes: bool = typer.Option(False, "--yes", help="Required to apply a write"),
    as_json: bool = typer.Option(False, "--json"),
    agent: str = typer.Option("rahul", "--agent"),
):
    """Log a touchpoint against every contact in the cohort."""
    if kind not in VALID_KINDS:
        err(f"'{kind}' is not a valid kind. Valid: {sorted(VALID_KINDS)}")
        raise typer.Exit(1)
    _validate_iso_date(date)
    occurred = date or date_t.today().isoformat()

    client = get_client()
    ids = _gate(client, status=status, tier=tier, tag=tag, affiliation=affiliation,
                cold_since=cold_since, all_=all_, dry_run=dry_run, yes=yes,
                as_json=as_json, agent=agent)
    if ids is None:  # gate already emitted (dry-run preview / empty cohort) or raised
        return

    for i in range(0, len(ids), CHUNK):
        chunk = ids[i:i + CHUNK]
        client.table("interactions").insert([
            {"contact_id": cid, "kind": kind, "channel": channel,
             "occurred_at": occurred, "summary": summary, "logged_by": agent}
            for cid in chunk
        ]).execute()

    # Monotonic bump of last_touchpoint_* fields; RPC is equal-date-safe and
    # chunks internally, so we pass the full id list.
    _bump_last_touchpoint_bulk(client, ids, occurred, channel, topic=summary)

    # changed == cohort for log (every matched contact gets an interaction row)
    _emit(ids, len(ids), dry_run=False, as_json=as_json)
