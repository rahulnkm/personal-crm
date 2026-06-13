# src/crm/commands/backfill.py
"""crm backfill — batched, parallel resolution of staged touchpoints.

Design: docs/superpowers/specs/2026-06-13-backfill-batching-design.md
Execution shape (the only change vs Plan 2 — behavior contracts hold):
  - pages of <=100 rows, ~10 bulk round-trips per page (was ~8 per ROW)
  - N worker threads; each wins rows via an atomic claim UPDATE and processes
    only the rows that UPDATE returned
  - workers never write contacts: last_touchpoint_* / closeness_tier are
    recomputed server-side from interactions (ground truth) by one RPC after
    the workers drain (this also heals any historical denorm staleness)
Constraint (unchanged from Plan 2): one `crm backfill` INVOCATION at a time —
the stale-claim reset at startup assumes no other invocation is mid-flight.
"""
import threading

import typer
from postgrest.exceptions import APIError

from crm.closeness import CHANNEL_TIER, TIER_RANK
from crm.commands.admin import require_agent
from crm.config import get_client
from crm.output import err

PAGE = 100          # bulk in_ filters travel in the URL; 100 ids ~ 4 KB, safe
MATCH_KEYS = ("email", "phone", "handle", "linkedin_url")
RECOMPUTE_CHUNK = 500
MAX_WORKERS = 16


def _claim_page(client) -> list[dict] | None:
    """Atomically win up to PAGE pending rows.

    Returns None when the queue is drained; [] when another worker stole this
    selection (caller must RETRY — more pending rows may exist past the window).
    """
    ids = [r["id"] for r in (client.table("staging_interactions").select("id")
           .eq("match_status", "pending").order("imported_at")
           .limit(PAGE).execute().data)]
    if not ids:
        return None
    return (client.table("staging_interactions")
            .update({"match_status": "claimed"})
            .in_("id", ids).eq("match_status", "pending")
            .execute().data)


def _bulk_match(client, rows: list[dict]) -> dict[str, str | None]:
    """staging row id -> contact_id (or None), preserving key priority order."""
    key_maps: dict[str, dict] = {}
    for key in MATCH_KEYS:
        values = sorted({r[key] for r in rows if r.get(key)})
        found: dict = {}
        for i in range(0, len(values), PAGE):
            for ident in (client.table("contact_identities")
                          .select(f"contact_id,{key}")
                          .in_(key, values[i:i + PAGE]).execute().data):
                found.setdefault(ident[key], ident["contact_id"])
        key_maps[key] = found
    return {r["id"]: next((key_maps[k][r[k]] for k in MATCH_KEYS
                           if r.get(k) and r[k] in key_maps[k]), None)
            for r in rows}


def _find_or_create_event(client, name, occurred_at, location, agent) -> str:
    q = client.table("events").select("id").eq("name", name).eq("source", "backfill")
    q = q.eq("occurred_at", occurred_at) if occurred_at else q.is_("occurred_at", "null")
    rows = q.limit(1).execute().data
    if rows:
        return rows[0]["id"]
    try:
        return client.table("events").insert(
            {"name": name, "occurred_at": occurred_at, "location": location,
             "source": "backfill", "created_by": agent}).execute().data[0]["id"]
    except APIError as exc:
        if getattr(exc, "code", None) != "23505":
            raise
        # another worker created it between our select and insert — use theirs
        return q.limit(1).execute().data[0]["id"]


def _process_page(client, rows: list[dict], agent: str) -> tuple[set, int, int]:
    """One batched page. Returns (touched contact ids, linked, orphaned)."""
    contact_by_row = _bulk_match(client, rows)

    # shared events: one find-or-create per unique (name, date) in this page
    event_ids: dict = {}
    for r in rows:
        if r.get("event_name") and contact_by_row[r["id"]]:
            key = (r["event_name"], r.get("occurred_at"))
            if key not in event_ids:
                event_ids[key] = _find_or_create_event(
                    client, r["event_name"], r.get("occurred_at"),
                    r.get("event_location"), agent)

    # bulk idempotency check (select-first: the unique index is partial,
    # PostgREST upserts cannot target it)
    ext_ids = sorted({r["source_external_id"] for r in rows})
    existing: dict = {}
    for i in range(0, len(ext_ids), PAGE):
        for it in (client.table("interactions")
                   .select("id,source,source_external_id")
                   .in_("source_external_id", ext_ids[i:i + PAGE]).execute().data):
            existing[(it["source"], it["source_external_id"])] = it["id"]

    inserts, patches, touched = [], [], set()
    linked = orphaned = 0
    for r in rows:
        contact_id = contact_by_row[r["id"]]
        # id stripped as hygiene — the (source, source_external_id) arbiter
        # resolves the conflict either way; minimal payload, obvious intent
        patch = {k: v for k, v in r.items() if k != "id"}
        if not contact_id:
            patch["match_status"] = "orphaned"
            orphaned += 1
            patches.append(patch)
            continue
        event_id = (event_ids.get((r["event_name"], r.get("occurred_at")))
                    if r.get("event_name") else None)
        hit = existing.get((r["source"], r["source_external_id"]))
        if hit:   # refresh contract: update in place, never duplicate
            client.table("interactions").update(
                {"occurred_at": r.get("occurred_at"), "summary": r.get("summary"),
                 "event_id": event_id, "contact_id": contact_id,
                 "updated_at": "now()"}).eq("id", hit).execute()
        else:
            inserts.append({"contact_id": contact_id, "event_id": event_id,
                            "kind": r["kind"], "channel": r.get("channel"),
                            "occurred_at": r.get("occurred_at"),
                            "summary": r.get("summary"), "logged_by": agent,
                            "source": r["source"],
                            "source_external_id": r["source_external_id"]})
        touched.add(contact_id)
        patch.update({"match_status": "linked", "matched_contact_id": contact_id,
                      "resolved_at": "now()"})
        linked += 1
        patches.append(patch)

    if inserts:
        client.table("interactions").insert(inserts).execute()
    if patches:
        client.table("staging_interactions").upsert(
            patches, on_conflict="source,source_external_id").execute()
    return touched, linked, orphaned


def _recompute(client, contact_ids: set) -> None:
    ids = sorted(contact_ids)
    for i in range(0, len(ids), RECOMPUTE_CHUNK):
        client.rpc("backfill_recompute_contacts",
                   {"contact_ids": ids[i:i + RECOMPUTE_CHUNK],
                    "channel_tier": CHANNEL_TIER,
                    "tier_rank": TIER_RANK}).execute()


def backfill(
    retry_orphans: bool = typer.Option(False, "--retry-orphans",
                                       help="Re-attempt previously orphaned rows"),
    workers: int = typer.Option(4, "--workers", help="Parallel page workers (1-16)"),
    agent: str = typer.Option("rahul", "--agent"),
):
    """Link all pending staged touchpoints to contacts (batched + parallel)."""
    workers = max(1, min(MAX_WORKERS, workers))
    client = get_client()
    require_agent(client, agent)
    stale = (client.table("staging_interactions")
             .update({"match_status": "pending"})
             .eq("match_status", "claimed").execute().data)
    if stale:
        err(f"reset {len(stale)} stale claims from a previous interrupted run")
    if retry_orphans:
        client.table("staging_interactions").update(
            {"match_status": "pending"}).eq("match_status", "orphaned").execute()

    lock = threading.Lock()
    state = {"touched": set(), "linked": 0, "orphaned": 0, "errors": []}

    def worker() -> None:
        wclient = get_client()   # one client per thread
        while True:
            try:
                page = _claim_page(wclient)
                if page is None:
                    return        # queue drained
                if not page:
                    continue      # selection stolen by another worker — retry
                touched, linked, orphaned = _process_page(wclient, page, agent)
                with lock:
                    state["touched"] |= touched
                    state["linked"] += linked
                    state["orphaned"] += orphaned
            except Exception as exc:  # noqa: BLE001 — this worker dies, others drain
                with lock:
                    state["errors"].append(f"{type(exc).__name__}: {exc}")
                return

    threads = [threading.Thread(target=worker, daemon=True) for _ in range(workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    if state["touched"]:
        _recompute(client, state["touched"])

    typer.echo(f"backfill: {state['linked']} linked, {state['orphaned']} orphaned "
               f"({workers} workers)")
    if state["orphaned"]:
        typer.echo("Orphans retry after importing more people: crm backfill --retry-orphans")
    if state["errors"]:
        err(f"{len(state['errors'])} worker error(s); rerun to resume. "
            f"First: {state['errors'][0]}")
        raise typer.Exit(1)
