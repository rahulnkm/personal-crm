# src/crm/commands/import_touchpoints.py
"""Generic touchpoints CSV → staging_interactions.

The agent-extraction path: Gmail/Telegram/Luma/Substack extractions produce a
CSV of dated touchpoints with at least one match key column (email / phone /
handle / linkedin_url); this stages them and `crm backfill` links them to
contacts. Per-row dates that fail ISO validation are skipped (counted), not
fatal — extractions are bulk and messy.
"""
import hashlib
import json
import re

import typer

from crm.commands.admin import require_agent
from crm.commands.import_csv import _read_rows, import_app
from crm.config import get_client
from crm.normalize import normalize_email, normalize_linkedin, normalize_phone
from crm.output import err

MATCH_KEYS = {"email", "phone", "handle", "linkedin_url"}
FIELDS = MATCH_KEYS | {"occurred_at", "summary", "event_name", "event_location",
                       "kind", "channel"}
VALID_KINDS = {"origin", "event", "email", "message", "call", "meeting"}
ISO = re.compile(r"\d{4}-\d{2}-\d{2}")
BATCH = 200


def _parse_tp_map(map_str: str) -> dict[str, str]:
    pairs = [p.split("=", 1) for p in map_str.split(",") if p]
    bad = [p for p in pairs if len(p) != 2]
    if bad:
        err(f"Malformed --map entries (need field=Header): {['='.join(p) for p in bad]}")
        raise typer.Exit(1)
    mapping = {k.strip(): v.strip() for k, v in pairs}
    unknown = set(mapping) - FIELDS
    if unknown:
        err(f"Unknown touchpoint fields in --map: {sorted(unknown)}. Allowed: {sorted(FIELDS)}")
        raise typer.Exit(1)
    if not (set(mapping) & MATCH_KEYS):
        err(f"--map must bind at least one match key: {sorted(MATCH_KEYS)}")
        raise typer.Exit(1)
    return mapping


@import_app.command("touchpoints")
def import_touchpoints(
    file: str = typer.Argument(..., help="CSV of dated touchpoints"),
    source: str = typer.Option(..., "--source", help="Source slug, e.g. tp_telegram"),
    map_str: str = typer.Option(..., "--map", help="field=Header,… (incl. a match key)"),
    kind: str = typer.Option(..., "--kind", help="Default kind for rows without one"),
    channel: str = typer.Option(..., "--channel", help="Default channel for rows without one"),
    agent: str = typer.Option("rahul", "--agent"),
):
    if kind not in VALID_KINDS:
        err(f"'{kind}' is not a valid kind. Valid: {sorted(VALID_KINDS)}")
        raise typer.Exit(1)
    client = get_client()
    require_agent(client, agent)
    mapping = _parse_tp_map(map_str)

    headers, rows = _read_rows(file)
    missing = [h for h in mapping.values() if h not in headers]
    if missing:
        err(f"CSV is missing mapped headers: {missing}. Headers found: {headers}")
        raise typer.Exit(1)

    staged, skipped, seen = [], 0, set()
    for raw in rows:
        rec = {field: (raw.get(header) or "").strip() or None
               for field, header in mapping.items()}
        rec["email"] = normalize_email(rec.get("email"))
        rec["phone"] = normalize_phone(rec.get("phone"))
        rec["linkedin_url"] = normalize_linkedin(rec.get("linkedin_url"))
        if not any(rec.get(k) for k in MATCH_KEYS):
            skipped += 1
            continue
        occurred = rec.get("occurred_at")
        if occurred and not ISO.fullmatch(occurred):
            skipped += 1
            continue
        row_kind = rec.pop("kind", None) or kind
        if row_kind not in VALID_KINDS:
            skipped += 1
            continue
        row_channel = rec.pop("channel", None) or channel
        digest = hashlib.sha256(json.dumps(raw, sort_keys=True).encode()).hexdigest()[:32]
        if (source, digest) in seen:   # dedupe within the file
            skipped += 1
            continue
        seen.add((source, digest))
        staged.append({**rec, "kind": row_kind, "channel": row_channel,
                       "source": source, "source_external_id": digest})

    for i in range(0, len(staged), BATCH):
        client.table("staging_interactions").upsert(
            staged[i:i + BATCH], on_conflict="source,source_external_id"
        ).execute()
    typer.echo(f"staged {len(staged)} touchpoints from {file} as source={source} "
               f"(skipped {skipped}). Next: crm backfill")
