"""crm import linkedin <zip|csv> — LinkedIn data-export Connections.

Your own export, downloaded with permission — the legally clean path (spec §12).
Emits BOTH staging people (name/email/profile/company/role) AND staged
touchpoints (kind=origin, channel=linkedin, occurred_at=Connected On): the
connection date is a real dated touchpoint.
"""
import csv
import hashlib
import io
import json
import zipfile
from datetime import datetime
from pathlib import Path

import typer

from crm.commands.admin import require_agent
from crm.commands.import_csv import import_app
from crm.config import get_client
from crm.normalize import normalize_email, normalize_linkedin
from crm.output import err

BATCH = 200
HEADER_PREFIX = "First Name,Last Name,URL"
# Ceiling on the uncompressed size of the Connections.csv we read into memory.
# A real export is KB–low-MB even for huge networks; this guards against a
# decompression bomb (tiny zip that inflates to many GB and OOMs the process).
MAX_MEMBER_BYTES = 200 * 1024 * 1024  # 200 MB


def _read_connections(path: str) -> list[dict]:
    p = Path(path)
    if not p.exists():
        err(f"File not found: {path}")
        raise typer.Exit(1)
    if p.suffix == ".zip":
        with zipfile.ZipFile(p) as zf:
            member = next((n for n in zf.namelist()
                           if n.endswith("Connections.csv")), None)
            if not member:
                err(f"No Connections.csv inside {path}. "
                    "Re-request the export with 'Connections' checked.")
                raise typer.Exit(1)
            declared = zf.getinfo(member).file_size
            if declared > MAX_MEMBER_BYTES:
                err(f"{member} is too large ({declared} bytes uncompressed, "
                    f"cap {MAX_MEMBER_BYTES}) — refusing to read a possible zip bomb.")
                raise typer.Exit(1)
            text = zf.read(member).decode("utf-8-sig")
    else:
        text = p.read_text(encoding="utf-8-sig")
    lines = text.splitlines()
    start = next((i for i, l in enumerate(lines) if l.startswith(HEADER_PREFIX)), None)
    if start is None:
        err("Could not find the Connections header row — unexpected export format.")
        raise typer.Exit(1)
    return list(csv.DictReader(io.StringIO("\n".join(lines[start:]))))


def _parse_connected_on(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return datetime.strptime(value.strip(), "%d %b %Y").date().isoformat()
    except ValueError:
        return None


@import_app.command("linkedin")
def import_linkedin(
    path: str = typer.Argument(..., help="Export zip or Connections.csv"),
    agent: str = typer.Option("rahul", "--agent"),
):
    client = get_client()
    require_agent(client, agent)
    rows = _read_connections(path)

    people, touchpoints, skipped, seen = [], [], 0, set()
    for raw in rows:
        full_name = " ".join(x for x in (
            (raw.get("First Name") or "").strip(),
            (raw.get("Last Name") or "").strip()) if x)
        if not full_name:
            skipped += 1
            continue
        url = normalize_linkedin(raw.get("URL"))
        digest = hashlib.sha256(json.dumps(raw, sort_keys=True).encode()).hexdigest()[:32]
        if ("linkedin", digest) in seen:
            skipped += 1
            continue
        seen.add(("linkedin", digest))
        people.append({
            "source": "linkedin", "source_external_id": digest,
            "full_name": full_name,
            "email": normalize_email(raw.get("Email Address")),
            "linkedin_url": url,
            "company": (raw.get("Company") or "").strip() or None,
            "role": (raw.get("Position") or "").strip() or None,
            "raw_json": raw,
        })
        occurred = _parse_connected_on(raw.get("Connected On"))
        if url and occurred:
            # touchpoint id keys on the PROFILE URL, not the row hash: a future
            # re-export with a changed Company/Position must refresh, not duplicate
            tp_digest = hashlib.sha256(f"linkedin:{url}".encode()).hexdigest()[:32]
            touchpoints.append({
                "source": "linkedin", "source_external_id": tp_digest,
                "match_status": "pending",  # re-pend on re-export so backfill refreshes
                "linkedin_url": url,
                "email": normalize_email(raw.get("Email Address")),
                "kind": "origin", "channel": "linkedin",
                "occurred_at": occurred,
                "summary": f"Connected on LinkedIn {occurred}",
            })

    for table, batch_rows in (("staging", people), ("staging_interactions", touchpoints)):
        for i in range(0, len(batch_rows), BATCH):
            client.table(table).upsert(
                batch_rows[i:i + BATCH], on_conflict="source,source_external_id"
            ).execute()
    typer.echo(f"staged {len(people)} connections + {len(touchpoints)} touchpoints "
               f"(skipped {skipped}). Next: crm dedup && crm backfill")
