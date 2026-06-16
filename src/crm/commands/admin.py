import json

import typer

from crm.config import get_client
from crm.output import err, render

agent_app = typer.Typer(help="Registry of writing agents. Register before writing.")
tags_app = typer.Typer(help="Tag registry — one definition per tag. Check before coining.")

# Tiers that count as a real connection. t1 = IRL/messaging, t2 = platform DMs.
PROMOTE_TIERS = ["t1_irl_messaging", "t2_dm"]
TIER_VALUES = {"t1_irl_messaging", "t2_dm", "t3_community", "t4_public", "none"}


def require_agent(client, agent_id: str) -> None:
    """Every mutating command calls this. Unregistered writer = exit 1."""
    try:
        rows = client.table("agents").select("id").eq("id", agent_id).execute().data
    except Exception as exc:
        err(f"Database unreachable: {exc}")
        raise typer.Exit(1)
    if not rows:
        err(f"Agent '{agent_id}' is not registered. "
            f"Run: crm agent register {agent_id} --desc '<one line: what this agent is>'")
        raise typer.Exit(1)
    # "now()" is parsed by Postgres as a timestamptz literal — the project-wide
    # pattern for server-side timestamps via PostgREST
    client.table("agents").update({"last_active": "now()"}).eq("id", agent_id).execute()


@agent_app.command("register")
def agent_register(
    agent_id: str = typer.Argument(..., help="Short slug, e.g. hiring-agent"),
    desc: str = typer.Option(..., "--desc", help="One line: what this agent is/does (its biases)"),
):
    client = get_client()
    client.table("agents").upsert(
        {"id": agent_id, "description": desc}, on_conflict="id"
    ).execute()
    typer.echo(f"registered: {agent_id}")


@agent_app.command("list")
def agent_list(as_json: bool = typer.Option(False, "--json")):
    rows = get_client().table("agents").select("id,description,last_active").execute().data
    render(rows, as_json)


@tags_app.command("add")
def tags_add(
    tag: str = typer.Argument(...),
    desc: str = typer.Option(..., "--desc", help="What the tag means and when to apply it"),
    agent: str = typer.Option("rahul", "--agent"),
):
    client = get_client()
    require_agent(client, agent)
    existing = client.table("tag_registry").select("tag,description").eq("tag", tag).execute().data
    if existing:
        err(f"Tag '{tag}' already exists: {existing[0]['description']}")
        raise typer.Exit(1)
    client.table("tag_registry").insert(
        {"tag": tag, "description": desc, "created_by": agent}
    ).execute()
    typer.echo(f"added tag: {tag}")


@tags_app.command("list")
def tags_list(as_json: bool = typer.Option(False, "--json")):
    rows = get_client().table("tag_registry").select("tag,description").execute().data
    render(rows, as_json)


def stats(as_json: bool = typer.Option(False, "--json")):
    """Coverage: contacts by status/tier, staging by match_status. Single
    crm_stats() RPC replaces the previous 16 head-count round-trips."""
    client = get_client()
    raw = client.rpc("crm_stats", {}).execute().data or {}
    # PostgREST may unwrap a single-row set-returning function into a list
    data: dict = raw if isinstance(raw, dict) else (raw[0] if raw else {})
    out = []
    for status in ("in_network", "contact_on_file"):
        out.append({"metric": f"connection_status={status}",
                    "count": (data.get("connection_status") or {}).get(status, 0)})
    for tier in ("t1_irl_messaging", "t2_dm", "t3_community", "t4_public", "none"):
        out.append({"metric": f"closeness_tier={tier}",
                    "count": (data.get("closeness_tier") or {}).get(tier, 0)})
    for ms in ("pending", "auto_matched", "needs_review", "merged", "rejected"):
        out.append({"metric": f"staging={ms}",
                    "count": (data.get("staging") or {}).get(ms, 0)})
    for ms in ("pending", "linked", "orphaned"):
        out.append({"metric": f"touchpoints={ms}",
                    "count": (data.get("touchpoints") or {}).get(ms, 0)})
    out.append({"metric": "contacts_total", "count": data.get("contacts_total", 0)})
    out = [o for o in out if o["count"] or o["metric"] == "contacts_total"]
    render(out, as_json)


def sync_status(
    tier: list[str] = typer.Option(
        None, "--tier",
        help="Tier(s) that count as a real connection (repeatable). Default: t1+t2."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Report the count without writing."),
    agent: str = typer.Option("rahul", "--agent"),
    as_json: bool = typer.Option(False, "--json"),
):
    """Promote contacts with a real touchpoint tier to in_network. Additive and
    idempotent: only flips contact_on_file rows, never demotes a manual in_network.
    Re-run after imports to mark newly-tiered people as real connections."""
    client = get_client()
    require_agent(client, agent)
    tiers = tier or list(PROMOTE_TIERS)
    bad = [t for t in tiers if t not in TIER_VALUES]
    if bad:
        err(f"Invalid tier(s) {bad}. Valid: {sorted(TIER_VALUES)}")
        raise typer.Exit(1)
    # head-count so the total stays exact past PostgREST's 1,000-row response cap
    n = (client.table("contacts").select("id", count="exact", head=True)
         .eq("connection_status", "contact_on_file").in_("closeness_tier", tiers)
         .execute().count or 0)
    if n and not dry_run:
        (client.table("contacts")
         .update({"connection_status": "in_network", "updated_at": "now()"})
         .eq("connection_status", "contact_on_file").in_("closeness_tier", tiers)
         .execute())
    result = {"promoted": n, "tiers": tiers, "dry_run": dry_run}
    if as_json:
        typer.echo(json.dumps(result))
    else:
        verb = "would promote" if dry_run else "promoted"
        typer.echo(f"{verb} {n} contact(s) to in_network (tiers: {', '.join(tiers)})")
