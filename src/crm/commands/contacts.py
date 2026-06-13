"""Golden-record commands. `contact` resolves a name or uuid; everything is --json-able."""
import json
import re
from datetime import date, timedelta

import typer

from crm.commands.admin import require_agent
from crm.config import get_client
from crm.output import err, render

SETTABLE = {
    "connection_status", "closeness_tier", "current_role", "current_company",
    "location", "origin_context", "email_status", "full_name",
    "affiliations", "tags",
}
ARRAY_FIELDS = {"affiliations", "tags"}

ENUM_VALUES = {
    "connection_status": {"in_network", "contact_on_file"},
    "closeness_tier": {"t1_irl_messaging", "t2_dm", "t3_community", "t4_public", "none"},
    "email_status": {"verified", "risky", "invalid", "unknown"},
}


def _resolve(client, ref: str) -> dict:
    """Accept a uuid or a (unique) name; exit 1 with candidates if ambiguous."""
    if len(ref) == 36 and ref.count("-") == 4:
        rows = client.table("contacts").select("*").eq("id", ref).execute().data
        if rows:
            return rows[0]
    pattern = ref.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_")
    rows = client.table("contacts").select("*").ilike("full_name", pattern).execute().data
    if len(rows) == 1:
        return rows[0]
    if not rows:
        fuzzy = client.rpc("match_contacts_by_name", {"q": ref, "lim": 3}).execute().data
        hint = ", ".join(f"{r['full_name']}" for r in fuzzy) or "none close"
        err(f"No contact '{ref}'. Closest: {hint}")
    else:
        err(f"Ambiguous '{ref}': " + ", ".join(f"{r['full_name']} ({r['id']})" for r in rows))
    raise typer.Exit(1)


def contact(ref: str = typer.Argument(..., help="Contact name or uuid"),
            as_json: bool = typer.Option(False, "--json")):
    """Full record: golden + identities + interactions + enrichment history."""
    client = get_client()
    c = _resolve(client, ref)
    idents = (client.table("contact_identities")
              .select("id,source,email,phone,linkedin_url,handle,imported_at")
              .eq("contact_id", c["id"]).execute().data)
    inter = (client.table("interactions")
             .select("kind,channel,occurred_at,summary,event_id,logged_by")
             .eq("contact_id", c["id"]).order("occurred_at", desc=True).execute().data)
    enrich = (client.table("enrichment_log")
              .select("field,old_value,new_value,source,created_at")
              .eq("contact_id", c["id"]).order("created_at", desc=True)
              .limit(20).execute().data)
    out = {"contact": c, "identities": idents, "interactions": inter,
           "enrichment_history": enrich}
    if as_json:
        typer.echo(json.dumps(out, default=str))
    else:
        typer.echo(f"{c['full_name']} — {c.get('current_role') or '?'} @ "
                   f"{c.get('current_company') or '?'} [{c['connection_status']}, "
                   f"{c['closeness_tier']}]")
        if c.get("origin_context"):
            typer.echo(f"  origin: {c['origin_context']}")
        if c.get("notes"):
            typer.echo(f"  notes: {c['notes']}")
        render(idents, False)
        render(inter, False)


def list_contacts(
    status: str = typer.Option(None, "--status"),
    tier: str = typer.Option(None, "--tier"),
    tag: str = typer.Option(None, "--tag"),
    affiliation: str = typer.Option(None, "--affiliation"),
    cold_since: int = typer.Option(None, "--cold-since",
                                   help="Months since last touchpoint (or never)"),
    limit: int = typer.Option(100, "--limit"),
    as_json: bool = typer.Option(False, "--json"),
):
    """The reconnection query. Filters compose with AND."""
    limit = min(limit, 1000)  # PostgREST response cap
    client = get_client()
    q = client.table("contacts").select(
        "id,full_name,current_role,current_company,connection_status,"
        "closeness_tier,affiliations,tags,last_touchpoint_at")
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
    rows = q.order("last_touchpoint_at", desc=False, nullsfirst=True).limit(limit).execute().data
    render(rows, as_json)


def search(query: str = typer.Argument(...),
           as_json: bool = typer.Option(False, "--json")):
    """Fuzzy name search + substring match on company/notes."""
    client = get_client()
    # RPC uses a bound parameter — injection-safe, raw query is fine (accents/commas OK there)
    fuzzy = client.rpc("match_contacts_by_name", {"q": query, "lim": 10}).execute().data
    ids = [r["contact_id"] for r in fuzzy]
    rows = []
    if ids:
        rows = (client.table("contacts")
                .select("id,full_name,current_role,current_company,connection_status")
                .in_("id", ids).execute().data)
    # PostgREST's or_() grammar treats , ( ) . * " % as syntax — neutralize them
    # so "Anderson, Inc" searches instead of crashing (or injecting clauses)
    safe = re.sub(r'[,().*"%]', " ", query).strip()
    subs = []
    if safe:
        subs = (client.table("contacts")
                .select("id,full_name,current_role,current_company,connection_status")
                .or_(f"current_company.ilike.%{safe}%,notes.ilike.%{safe}%")
                .limit(10).execute().data)
    seen = {r["id"] for r in rows}
    rows += [s for s in subs if s["id"] not in seen]
    render(rows, as_json)


def add(
    full_name: str = typer.Argument(...),
    status: str = typer.Option("contact_on_file", "--status"),
    tier: str = typer.Option("none", "--tier"),
    affiliation: list[str] = typer.Option([], "--affiliation"),
    role: str = typer.Option(None, "--role"),
    company: str = typer.Option(None, "--company"),
    email: str = typer.Option(None, "--email"),
    origin: str = typer.Option(None, "--origin", help="How/where connected"),
    agent: str = typer.Option("rahul", "--agent"),
):
    """Directly add a person (e.g. a campaign agent adding a scraped contact)."""
    client = get_client()
    require_agent(client, agent)
    if status not in ENUM_VALUES["connection_status"]:
        err(f"'{status}' is not a valid connection_status. Valid: {sorted(ENUM_VALUES['connection_status'])}")
        raise typer.Exit(1)
    if tier not in ENUM_VALUES["closeness_tier"]:
        err(f"'{tier}' is not a valid closeness_tier. Valid: {sorted(ENUM_VALUES['closeness_tier'])}")
        raise typer.Exit(1)
    c = client.table("contacts").insert(
        {"full_name": full_name, "connection_status": status, "closeness_tier": tier,
         "affiliations": affiliation, "current_role": role,
         "current_company": company, "origin_context": origin}
    ).execute().data[0]
    try:
        client.table("contact_identities").insert(
            {"contact_id": c["id"], "source": f"agent:{agent}", "email": email}
        ).execute()
    except Exception as exc:
        err(f"contact created ({c['id']}) but identity insert failed: {exc}")
    typer.echo(c["id"])


def set_field(
    ref: str = typer.Argument(...),
    assignment: str = typer.Argument(..., help="field=value; array fields append"),
    agent: str = typer.Option("rahul", "--agent"),
):
    """crm set <contact> connection_status=in_network — the toggle, agent-writable."""
    client = get_client()
    require_agent(client, agent)
    if "=" not in assignment:
        err("Expected field=value")
        raise typer.Exit(2)
    field, value = assignment.split("=", 1)
    if field not in SETTABLE:
        err(f"'{field}' is not settable. Settable: {sorted(SETTABLE)}")
        raise typer.Exit(1)
    if field in ENUM_VALUES and value not in ENUM_VALUES[field]:
        err(f"'{value}' is not a valid {field}. Valid: {sorted(ENUM_VALUES[field])}")
        raise typer.Exit(1)
    c = _resolve(client, ref)
    if field in ARRAY_FIELDS:
        if field == "tags":
            known = client.table("tag_registry").select("tag").eq("tag", value).execute().data
            if not known:
                err(f"Tag '{value}' not in registry. First: crm tags add {value} --desc '...'")
                raise typer.Exit(1)
        new_array = sorted(set(c[field]) | {value})
        update = {field: new_array}
    else:
        update = {field: value}
    old = c.get(field)
    update["updated_at"] = "now()"
    client.table("contacts").update(update).eq("id", c["id"]).execute()
    client.table("enrichment_log").insert(
        {"contact_id": c["id"], "field": field, "old_value": str(old),
         "new_value": str(update[field]), "source": agent, "method": "manual_set"}
    ).execute()
    typer.echo(f"{c['full_name']}: {field} = {update[field]}")


def note(
    ref: str = typer.Argument(...),
    text: str = typer.Argument(...),
    agent: str = typer.Option("rahul", "--agent"),
):
    """Append a dated note to the contact's freeform notes."""
    client = get_client()
    require_agent(client, agent)
    c = _resolve(client, ref)
    # attribution prefix is informational, not tamper-proof — notes are freeform by design (authoritative audit lives in enrichment_log for set/merge/split)
    stamped = f"[{date.today().isoformat()} {agent}] {text}"
    notes = (c.get("notes") + "\n" + stamped) if c.get("notes") else stamped
    client.table("contacts").update(
        {"notes": notes, "updated_at": "now()"}).eq("id", c["id"]).execute()
    typer.echo("noted")
