# src/crm/commands/import_imessage.py
"""crm import imessage — message-history touchpoints from the macOS chat.db.

Aggregates per handle (phone/email): message count + most recent date, staged
as touchpoints (kind=message, channel=imessage → tier-1 evidence). Emits NO
people — run `crm import apple-contacts && crm dedup` first so handles match;
leftovers orphan and recover via `crm backfill --retry-orphans`.
"""
import hashlib
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import typer

from crm.commands.admin import require_agent
from crm.commands.import_csv import import_app
from crm.config import get_client
from crm.normalize import normalize_email, normalize_phone
from crm.output import AGENT_HELP, err

BATCH = 200
APPLE_EPOCH_OFFSET = 978_307_200  # 2001-01-01 vs 1970-01-01, seconds
DEFAULT_DB = Path.home() / "Library" / "Messages" / "chat.db"


def _apple_ns_to_date(ns: int | None) -> str | None:
    if not ns:
        return None
    # very old rows stored seconds; modern rows store nanoseconds
    seconds = ns / 1_000_000_000 if ns > 10_000_000_000 else ns
    return datetime.fromtimestamp(seconds + APPLE_EPOCH_OFFSET,
                                  tz=timezone.utc).date().isoformat()


@import_app.command("imessage")
def import_imessage(
    db_path: str = typer.Option(str(DEFAULT_DB), "--db", help="Path to chat.db"),
    agent: str = typer.Option("rahul", "--agent", help=AGENT_HELP),
):
    """Stage iMessage history as touchpoints. Run import apple-contacts + dedup FIRST."""
    client = get_client()
    require_agent(client, agent)
    path = Path(db_path)
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        rows = con.execute(
            "select h.id, count(*), max(m.date) from message m "
            "join handle h on m.handle_id = h.ROWID group by h.id").fetchall()
        con.close()
    except sqlite3.OperationalError as exc:
        err(f"Cannot read {path}: {exc}\n"
            "Grant Full Disk Access to this app (System Settings → Privacy & "
            "Security → Full Disk Access), or pass --db <path>.")
        raise typer.Exit(1)

    staged, skipped = [], 0
    for handle, count, last_ns in rows:
        email = normalize_email(handle)
        phone = normalize_phone(handle)
        occurred = _apple_ns_to_date(last_ns)
        if not (email or phone) or not occurred:
            skipped += 1
            continue
        # digest keyed on handle only → stable across re-imports
        digest = hashlib.sha256(f"imessage:{handle}".encode()).hexdigest()[:32]
        staged.append({
            "source": "imessage", "source_external_id": digest,
            "email": email, "phone": phone,
            "kind": "message", "channel": "imessage",
            "occurred_at": occurred,
            "summary": f"{count} messages on iMessage; latest {occurred}",
            # reset to pending so backfill refreshes already-linked rows
            "match_status": "pending",
        })
    for i in range(0, len(staged), BATCH):
        client.table("staging_interactions").upsert(
            staged[i:i + BATCH], on_conflict="source,source_external_id").execute()
    typer.echo(f"staged {len(staged)} handle touchpoints from {path} "
               f"(skipped {skipped}). Next: crm backfill")
