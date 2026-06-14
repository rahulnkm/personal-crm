"""Retrieval surface for in-context semantic matching.

`crm capsules` emits one compact object per contact — the searchable representation
Claude Code reads to reason over the network. No embeddings, no LLM calls here.
"""
import json

import typer

from crm.commands.contacts import apply_contact_filters
from crm.config import get_client

# How far past PostgREST's 1,000-row response cap we page when materializing the
# full capsule set. range() asks for [start, end] inclusive.
PAGE = 1000
NOTE_MAX = 140      # capsule note truncation budget (chars)
TOPICS_MAX = 2      # top-N recent interaction summaries per capsule

CAPSULE_COLS = (
    "id,full_name,current_role,current_company,company_category,location,"
    "closeness_tier,affiliations,tags,expertise,notes,last_touchpoint_at"
)


def _truncate(text: str | None, limit: int = NOTE_MAX) -> str:
    if not text:
        return ""
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "…"


def _page_all(client, base_query_fn) -> list[dict]:
    """Page a filtered contacts query past the 1,000-row cap via range().

    base_query_fn(client) must return a fresh PostgREST builder each call (filters
    already applied) so each page is an independent request.
    """
    rows: list[dict] = []
    start = 0
    while True:
        page = (base_query_fn()
                .order("id")
                .range(start, start + PAGE - 1)
                .execute().data)
        rows.extend(page)
        if len(page) < PAGE:
            break
        start += PAGE
    return rows


def _topics_by_contact(client, contact_ids: list[str]) -> dict[str, list[str]]:
    """Top-N recent interaction summaries per contact, newest first.

    One query for all ids (chunked), sorted desc; we keep the first N non-empty
    summaries per contact client-side.
    """
    out: dict[str, list[str]] = {cid: [] for cid in contact_ids}
    if not contact_ids:
        return out
    for i in range(0, len(contact_ids), 200):  # keep the IN list well under URL limits
        chunk = contact_ids[i:i + 200]
        rows = (client.table("interactions")
                .select("contact_id,summary,occurred_at,created_at")
                .in_("contact_id", chunk)
                .order("occurred_at", desc=True, nullsfirst=False)
                .order("created_at", desc=True)
                .execute().data)
        for r in rows:
            cid = r["contact_id"]
            summary = (r.get("summary") or "").strip()
            if summary and len(out[cid]) < TOPICS_MAX:
                out[cid].append(summary)
    return out


def _stale_by_contact(client, contact_ids: list[str]) -> dict[str, bool]:
    """A contact is 'stale' if any current enrichment field is past its refresh_after."""
    from datetime import date
    today = date.today().isoformat()
    out: dict[str, bool] = {cid: False for cid in contact_ids}
    if not contact_ids:
        return out
    for i in range(0, len(contact_ids), 200):
        chunk = contact_ids[i:i + 200]
        rows = (client.table("enrichment_log")
                .select("contact_id,refresh_after")
                .in_("contact_id", chunk)
                .eq("is_current", True)
                .not_.is_("refresh_after", "null")
                .execute().data)
        for r in rows:
            if r["refresh_after"] and r["refresh_after"] < today:
                out[r["contact_id"]] = True
    return out


def _capsule(c: dict, topics: list[str], stale: bool) -> dict:
    return {
        "name": c["full_name"],
        "role": c.get("current_role"),
        "company": c.get("current_company"),
        "company_category": c.get("company_category"),
        "expertise": c.get("expertise") or [],
        "tags": c.get("tags") or [],
        "note": _truncate(c.get("notes")),
        "topics": topics,
        "location": c.get("location"),
        "tier": c.get("closeness_tier"),
        "last": c.get("last_touchpoint_at"),
        "stale": stale,
    }


def _build_capsules(client, contacts: list[dict]) -> list[dict]:
    ids = [c["id"] for c in contacts]
    topics = _topics_by_contact(client, ids)
    stale = _stale_by_contact(client, ids)
    return [_capsule(c, topics.get(c["id"], []), stale.get(c["id"], False))
            for c in contacts]


def capsules(
    status: str = typer.Option(None, "--status"),
    tier: str = typer.Option(None, "--tier"),
    tag: str = typer.Option(None, "--tag"),
    affiliation: str = typer.Option(None, "--affiliation"),
    role: str = typer.Option(None, "--role"),
    role_class: str = typer.Option(None, "--role-class"),
    company_category: str = typer.Option(None, "--company-category"),
    location: str = typer.Option(None, "--location"),
    cold_since: int = typer.Option(None, "--cold-since"),
    as_json: bool = typer.Option(False, "--json"),
):
    """Compact capsule per contact — the searchable representation for in-context match.

    Accepts the same structured filters as `crm list` to pre-narrow the set. Pages
    past PostgREST's 1,000-row response cap so the full network is materialized.
    """
    client = get_client()

    def query():
        q = client.table("contacts").select(CAPSULE_COLS)
        return apply_contact_filters(
            q, status=status, tier=tier, tag=tag, affiliation=affiliation,
            cold_since=cold_since, role=role, role_class=role_class,
            company_category=company_category, location=location)

    contacts = _page_all(client, query)
    caps = _build_capsules(client, contacts)
    if as_json:
        typer.echo(json.dumps(caps, default=str))
    else:
        for cap in caps:
            typer.echo(json.dumps(cap, default=str))
