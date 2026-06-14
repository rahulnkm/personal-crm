"""crm dedup — resolve pending staging rows into golden records.

Survivorship (v1, deliberately simple): existing non-null golden values win;
incoming values fill nulls; conflicts are logged to enrichment_log so nothing
is silently dropped. Identities are never destroyed (XREF) — merge/split stay cheap.
"""
import json
import threading

import typer

from crm.closeness import TIER_RANK
from crm.commands.admin import require_agent
from crm.config import get_client
from crm.dedup_plan import IDENTITY_FIELDS, build_plan
from crm.matching import _is_role_email, classify, find_candidates
from crm.output import err, render

FILL_FIELDS = {"current_role": "role", "current_company": "company",
               "location": "location"}
PAGE = 1000  # PostgREST caps responses at 1,000 — page until drained
MAX_WORKERS = 16
FILL = FILL_FIELDS


def _load_pending(client):
    out = []
    while True:
        rows = (client.table("staging").select("*").eq("match_status", "pending")
                .order("imported_at").range(len(out), len(out) + PAGE - 1).execute().data)
        out += rows
        if len(rows) < PAGE:
            return out


def _fill_and_log(client, contact_id, staged, source):
    contact = client.table("contacts").select("*").eq("id", contact_id).single().execute().data
    updates, conflicts = {}, []
    for cf, sf in FILL.items():
        new = staged.get(sf)
        if not new:
            continue
        if not contact.get(cf):
            updates[cf] = new
        elif contact[cf] != new:
            conflicts.append((cf, contact[cf], new))
    if staged.get("full_name") and staged["full_name"] != contact["full_name"]:
        conflicts.append(("full_name", contact["full_name"], staged["full_name"]))
    if updates:
        updates["updated_at"] = "now()"
        client.table("contacts").update(updates).eq("id", contact_id).execute()
    if conflicts:
        client.table("enrichment_log").insert(
            [{"contact_id": contact_id, "field": f, "old_value": o, "new_value": n,
              "source": source, "method": "import_conflict"} for f, o, n in conflicts]).execute()


def _patch(it, status, contact_id, conf=None, method=None, resolved=True):
    p = {"source": it["source"], "source_external_id": it["source_external_id"],
         "dedup_cluster": it["cluster_id"], "match_status": status,
         "match_method": method or it.get("match_method")}
    if contact_id:
        p["matched_contact_id"] = contact_id
    if conf is not None:
        p["match_confidence"] = conf
    if resolved:
        p["resolved_at"] = "now()"
    return p


def _bump(state, lock, key):
    with lock:
        state[key] += 1


def _fold_auto(auto, deref, contact_by_id, existing):
    """PURE in-memory replay of the sequential auto_matched path (no DB).

    Walks `auto` items in plan order and reproduces _attach_identity + _fill_and_log
    serial semantics against an in-memory per-contact accumulator, so later items in a
    cluster see earlier items' fills (the load-bearing cross-item guarantee).

    Args:
        auto: auto_matched plan items, in PLAN ORDER.
        deref: create_key→uuid resolver (identity for already-real uuids).
        contact_by_id: {contact_uuid: contact-row dict} for every dereferenced target.
        existing: {(source, source_external_id): contact_id} prefetched DB identity map.

    Returns:
        id_inserts: list of contact_identities rows to insert-or-ignore.
        enrich_rows: list of enrichment_log rows (import_conflict).
        fills: {contact_id: {field: value}} fill-null updates, one per contact.
        outcomes: {item_id: "attached" | "conflict"}.
    """
    acc = {cid: dict(row) for cid, row in contact_by_id.items()}  # mutable per-contact state
    seen_identities = dict(existing)              # DB map + in-cluster queued inserts
    id_inserts, enrich_rows, fills, outcomes = [], [], {}, {}
    for it in auto:
        cid = deref(it["matched_ref"])
        ident = it["identity"]
        staged = it["staged"]
        k = (ident.get("source"), ident.get("source_external_id"))
        if ident.get("source_external_id") and k in seen_identities:
            if seen_identities[k] != cid:
                outcomes[it["id"]] = "conflict"      # identity lives on another contact
                continue
            # same contact: no re-insert; fall through to fill
        elif ident.get("source_external_id"):
            id_inserts.append({"contact_id": cid,
                               **{f: ident.get(f) for f in IDENTITY_FIELDS}})
            seen_identities[k] = cid
        # FILL against the accumulator (mirrors the per-item DB re-read), write-back so
        # later items in this cluster see what earlier items filled.
        for cf, sf in FILL.items():
            new = staged.get(sf)
            if not new:
                continue
            if not acc[cid].get(cf):
                fills.setdefault(cid, {})[cf] = new
                acc[cid][cf] = new                   # WRITE BACK
            elif acc[cid][cf] != new:
                enrich_rows.append({"contact_id": cid, "field": cf, "old_value": acc[cid][cf],
                                    "new_value": new, "source": it["source"],
                                    "method": "import_conflict"})
        if staged.get("full_name") and staged["full_name"] != acc[cid].get("full_name"):
            enrich_rows.append({"contact_id": cid, "field": "full_name",
                                "old_value": acc[cid].get("full_name"),
                                "new_value": staged["full_name"], "source": it["source"],
                                "method": "import_conflict"})
        outcomes[it["id"]] = "attached"
    return id_inserts, enrich_rows, fills, outcomes


def _execute_cluster(client, items, state, lock):
    keymap = {}
    creates = [it for it in items if it.get("create_key")]
    if creates:
        payload = [{"create_key": it["create_key"], "contact": it["contact_fields"],
                    "identity": {f: it["identity"].get(f) for f in IDENTITY_FIELDS}}
                   for it in creates]
        for row in (client.rpc("create_contacts_with_identities",
                    {"payload": payload}).execute().data):
            keymap[row["create_key"]] = row["contact_id"]

    def deref(ref):
        return keymap.get(ref, ref)

    # ---- auto_matched branch: batched reads → in-memory fold → batched writes ----
    auto = [it for it in items if it["match_status"] == "auto_matched"]
    cids = sorted({deref(it["matched_ref"]) for it in auto})
    contact_by_id = {c["id"]: c for c in
                     client.table("contacts").select("*").in_("id", cids).execute().data} \
        if cids else {}
    sxids = sorted({it["identity"]["source_external_id"] for it in auto
                    if it["identity"].get("source_external_id")})
    existing = {}                                  # (source, source_external_id) -> contact_id
    for i in range(0, len(sxids), 100):
        for r in (client.table("contact_identities")
                  .select("source,source_external_id,contact_id")
                  .in_("source_external_id", sxids[i:i + 100]).execute().data):
            existing[(r["source"], r["source_external_id"])] = r["contact_id"]

    id_inserts, enrich_rows, fills, outcomes = _fold_auto(
        auto, deref, contact_by_id, existing)

    if id_inserts:
        # insert-or-ignore via RPC: PostgREST .upsert() can't target the PARTIAL
        # unique index on (source, source_external_id), and ON CONFLICT DO NOTHING
        # guarantees a duplicate/pre-existing identity never aborts the batch.
        client.rpc("bulk_insert_identities", {"payload": id_inserts}).execute()
    if enrich_rows:
        client.table("enrichment_log").insert(enrich_rows).execute()
    for cid, upd in fills.items():
        client.table("contacts").update({**upd, "updated_at": "now()"}).eq("id", cid).execute()

    patches = []
    for it in items:
        st = it["match_status"]
        if st == "rejected":
            patches.append(_patch(it, "rejected", None, method="no_name"))
            _bump(state, lock, "rejected")
        elif it.get("create_key"):
            patches.append(_patch(it, "merged", keymap[it["create_key"]], method="new_contact"))
            _bump(state, lock, "created")
        elif st == "auto_matched":
            cid = deref(it["matched_ref"])
            if outcomes[it["id"]] == "attached":
                patches.append(_patch(it, "auto_matched", cid,
                                      conf=it.get("match_confidence"), method=it["match_method"]))
                _bump(state, lock, "auto")
            else:                                          # rerun_conflict → review
                patches.append(_patch(it, "needs_review", cid,
                                      method="rerun_conflict", resolved=False))
                _bump(state, lock, "review")
        elif st == "needs_review":
            cid = deref(it.get("matched_ref"))
            patches.append(_patch(it, "needs_review", cid, conf=it.get("match_confidence"),
                                  method=it["match_method"], resolved=False))
            _bump(state, lock, "review")
    for i in range(0, len(patches), 200):
        client.table("staging").upsert(
            patches[i:i + 200], on_conflict="source,source_external_id").execute()


def _attach(client, staged: dict, contact_id: str) -> bool:
    """Sequential single-row attach — used by `crm review --approve`. Idempotent
    select-first identity guard (rerun_conflict patches staging + returns False),
    then fill-null + conflict-log. The bulk dedup path uses the batched _fold_auto
    fold instead; this preserves the manual-review callsite unchanged."""
    existing = []
    if staged.get("source_external_id"):
        existing = (client.table("contact_identities")
                    .select("id,contact_id")
                    .eq("source", staged["source"])
                    .eq("source_external_id", staged["source_external_id"])
                    .execute().data)
    if existing:
        if existing[0]["contact_id"] != contact_id:
            client.table("staging").update(
                {"match_status": "needs_review",
                 "matched_contact_id": contact_id,
                 "match_method": "rerun_conflict"}
            ).eq("id", staged["id"]).execute()
            return False
    else:
        client.table("contact_identities").insert(
            {"contact_id": contact_id,
             **{f: staged.get(f) for f in IDENTITY_FIELDS}}
        ).execute()
    _fill_and_log(client, contact_id, staged, staged["source"])
    return True


def _create(client, staged: dict) -> str:
    contact = client.table("contacts").insert(
        {"full_name": staged["full_name"],
         "current_role": staged.get("role"),
         "current_company": staged.get("company"),
         "location": staged.get("location")}
    ).execute().data[0]
    client.table("contact_identities").insert(
        {"contact_id": contact["id"],
         **{f: staged.get(f) for f in IDENTITY_FIELDS}}
    ).execute()
    return contact["id"]


def dedup(workers: int = typer.Option(4, "--workers", help="Parallel workers (1-16)"),
          agent: str = typer.Option("rahul", "--agent")):
    """Two-phase dedup: serial plan, parallel execute. A cluster is a write-isolation
    unit; verdicts replay the sequential engine. Crash-resume is identity-keyed
    (rerun re-plans pending rows; atomic-create + select-first prevent dupes)."""
    workers = max(1, min(MAX_WORKERS, workers))
    client = get_client()
    require_agent(client, agent)
    pending = _load_pending(client)
    if not pending:
        typer.echo("dedup: nothing pending")
        return
    plan = build_plan(client, pending)
    by_cluster = {}
    for p in plan:
        by_cluster.setdefault(p["cluster_id"], []).append(p)
    cluster_ids = list(by_cluster)
    cursor, lock = {"i": 0}, threading.Lock()
    state = {"created": 0, "auto": 0, "review": 0, "rejected": 0, "errors": []}

    def worker():
        wc = get_client()
        while True:
            with lock:
                if cursor["i"] >= len(cluster_ids):
                    return
                cid = cluster_ids[cursor["i"]]
                cursor["i"] += 1
            try:
                _execute_cluster(wc, by_cluster[cid], state, lock)
            except Exception as exc:  # noqa: BLE001
                with lock:
                    state["errors"].append(f"{type(exc).__name__}: {exc}")
                return

    threads = [threading.Thread(target=worker, daemon=True) for _ in range(workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    typer.echo(f"dedup: {state['created']} created, {state['auto']} attached, "
               f"{state['review']} queued for review, {state['rejected']} rejected "
               f"({workers} workers)")
    if state["review"]:
        typer.echo("Next: crm review")
    if state["errors"]:
        err(f"{len(state['errors'])} worker error(s); rerun to resume. First: {state['errors'][0]}")
        raise typer.Exit(1)


def _candidate_display(client, q: dict) -> str:
    """Return a human-readable candidate string for a review queue row.

    For conflicting_keys / rerun_conflict: re-derive ALL distinct contact_ids
    by querying contact_identities for every key the staged row has, then
    render them as "Name (Co), Name2 (Co2)".
    For fuzzy_name: single lookup of matched_contact_id → "Name (Co or ?)".
    Handles missing/deleted contacts gracefully with "<gone>".
    """
    conflict_methods = ("conflicting_keys", "rerun_conflict")
    if q.get("match_method") in conflict_methods:
        # Re-derive all candidates from the staged row's identity keys
        candidate_ids: set[str] = set()
        for field in ("email", "linkedin_url", "phone"):
            val = q.get(field)
            if not val:
                continue
            if field == "email" and _is_role_email(val):
                continue
            rows = (client.table("contact_identities")
                    .select("contact_id")
                    .eq(field, val)
                    .execute().data)
            for r in rows:
                candidate_ids.add(r["contact_id"])
        # Also include the stored matched_contact_id if present
        if q.get("matched_contact_id"):
            candidate_ids.add(q["matched_contact_id"])
        if not candidate_ids:
            return "<no candidates>"
        parts = []
        for cid in candidate_ids:
            contacts = (client.table("contacts")
                        .select("full_name,current_company")
                        .eq("id", cid).execute().data)
            if not contacts:
                parts.append("<gone>")
            else:
                c = contacts[0]
                co = c.get("current_company") or "?"
                parts.append(f"{c['full_name']} ({co})")
        return ", ".join(parts)
    else:
        # Fuzzy match: single candidate
        cid = q.get("matched_contact_id")
        if not cid:
            return "<none>"
        contacts = (client.table("contacts")
                    .select("full_name,current_company")
                    .eq("id", cid).execute().data)
        if not contacts:
            return "<gone>"
        c = contacts[0]
        co = c.get("current_company") or "?"
        return f"{c['full_name']} ({co})"


def review(
    approve: str = typer.Option(None, "--approve", help="staging id: confirm the match"),
    reject: str = typer.Option(None, "--reject", help="staging id: not the same person"),
    to: str = typer.Option(None, "--to", help="contact id to attach to (overrides stored candidate)"),
    as_json: bool = typer.Option(False, "--json"),
    agent: str = typer.Option("rahul", "--agent"),
):
    """List the clerical-review queue, or resolve one row."""
    client = get_client()
    if approve and reject:
        err("Pass --approve OR --reject, not both.")
        raise typer.Exit(2)
    if approve or reject:
        require_agent(client, agent)
        sid = approve or reject
        rows = client.table("staging").select("*").eq("id", sid).execute().data
        if not rows or rows[0]["match_status"] != "needs_review":
            err(f"No needs_review staging row with id {sid}")
            raise typer.Exit(1)
        staged = rows[0]
        if approve:
            # guard: candidate contact may have been merged away (FK set null on delete)
            if not staged.get("matched_contact_id") and not to:
                err("Candidate contact no longer exists — use `crm review --approve <id> --to <contact_id>` "
                    "to pick a different contact, or `crm review --reject <id>` to create a new one.")
                raise typer.Exit(1)
            # --to overrides the stored candidate
            target_id = to or staged["matched_contact_id"]
            if to:
                exists = client.table("contacts").select("id").eq("id", to).execute().data
                if not exists:
                    err(f"Contact {to} does not exist.")
                    raise typer.Exit(1)
            if _attach(client, staged, target_id):
                client.table("staging").update(
                    {"match_status": "merged", "resolved_at": "now()"}
                ).eq("id", sid).execute()
                typer.echo("approved")
            else:
                err("conflict persists — see crm review")
                raise typer.Exit(1)
        else:
            # guard: rerun_conflict row's identity already lives on another contact
            if rows[0].get("match_method") == "rerun_conflict":
                err("This source row's identity already exists on another contact — "
                    "use `crm split` on that contact instead of reject.")
                raise typer.Exit(1)
            cid = _create(client, staged)
            # on rejected rows matched_contact_id means "resulting contact",
            # not "candidate match" — it points at the NEW contact we created
            patch = {"match_status": "rejected", "matched_contact_id": cid,
                     "resolved_at": "now()"}
            client.table("staging").update(patch).eq("id", sid).execute()
            typer.echo("rejected (new contact created)")
        return
    queue = (client.table("staging").select(
        "id,full_name,email,linkedin_url,phone,company,source,"
        "match_confidence,match_method,matched_contact_id")
        .eq("match_status", "needs_review")
        .order("match_confidence", desc=True).execute().data)
    for q in queue:
        q["candidate"] = _candidate_display(client, q)
    render(queue, as_json)
    if queue and not as_json:
        typer.echo("\nResolve: crm review --approve <id> | crm review --reject <id>")


def merge(
    keep_id: str = typer.Argument(..., help="Contact to keep"),
    drop_id: str = typer.Argument(..., help="Contact to fold into the kept one"),
    agent: str = typer.Option("rahul", "--agent"),
):
    """Manually fuse two contacts the matcher kept apart (under-merge fix)."""
    client = get_client()
    require_agent(client, agent)
    if keep_id == drop_id:
        err("keep and drop are the same contact — nothing to merge.")
        raise typer.Exit(2)
    drop = client.table("contacts").select("*").eq("id", drop_id).execute().data
    keep = client.table("contacts").select("*").eq("id", keep_id).execute().data
    if not drop or not keep:
        err("Both contact ids must exist.")
        raise typer.Exit(1)
    for table in ("contact_identities", "interactions", "enrichment_log"):
        client.table(table).update({"contact_id": keep_id}).eq(
            "contact_id", drop_id).execute()
    keep_c, drop_c = keep[0], drop[0]
    updates = {f: drop_c[f] for f in
               ("current_role", "current_company", "location", "origin_context")
               if drop_c.get(f) and not keep_c.get(f)}
    # arrays: union, never drop
    for arr in ("tags", "affiliations"):
        merged = sorted(set(keep_c.get(arr) or []) | set(drop_c.get(arr) or []))
        if merged != (keep_c.get(arr) or []):
            updates[arr] = merged
    # notes: append, never overwrite or lose
    if drop_c.get("notes"):
        updates["notes"] = ((keep_c.get("notes") or "") +
                            ("\n" if keep_c.get("notes") else "") +
                            f"[merged from {drop_c['full_name']}] {drop_c['notes']}")
    # closeness: most intimate wins; status: in_network wins
    if TIER_RANK.get(drop_c["closeness_tier"], 0) > TIER_RANK.get(keep_c["closeness_tier"], 0):
        updates["closeness_tier"] = drop_c["closeness_tier"]
    if drop_c["connection_status"] == "in_network" and keep_c["connection_status"] != "in_network":
        updates["connection_status"] = "in_network"
    if updates:
        client.table("contacts").update(updates).eq("id", keep_id).execute()
    client.table("enrichment_log").insert(
        {"contact_id": keep_id, "field": "_merge", "old_value": drop_id,
         "new_value": json.dumps(drop_c, default=str),  # full row — merge is hand-reversible
         "source": agent, "method": "manual_merge"}
    ).execute()
    client.table("contacts").delete().eq("id", drop_id).execute()
    typer.echo(f"merged {drop[0]['full_name']} into {keep[0]['full_name']}")


def split(
    contact_id: str = typer.Argument(...),
    identity_id: str = typer.Argument(..., help="Identity to detach into a new contact"),
    agent: str = typer.Option("rahul", "--agent"),
):
    """Detach a wrongly-merged identity into its own contact (over-merge fix)."""
    client = get_client()
    require_agent(client, agent)
    idents = (client.table("contact_identities").select("*")
              .eq("id", identity_id).eq("contact_id", contact_id).execute().data)
    if not idents:
        err("Identity not found on that contact.")
        raise typer.Exit(1)
    ident = idents[0]
    raw = ident.get("raw_json") or {}
    name = raw.get("full_name") or raw.get("Name") or "UNKNOWN — fix via crm set"
    new = client.table("contacts").insert({"full_name": name}).execute().data[0]
    client.table("contact_identities").update(
        {"contact_id": new["id"]}).eq("id", identity_id).execute()
    client.table("enrichment_log").insert(
        {"contact_id": new["id"], "field": "_split", "old_value": contact_id,
         "new_value": identity_id, "source": agent, "method": "manual_split"}
    ).execute()
    typer.echo(f"split identity {identity_id} into new contact {new['id']} ({name})")
