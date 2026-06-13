"""crm import apple-contacts — people from the macOS AddressBook sqlite.

Emits one staging row per (person × email/phone); dedup folds them into one
golden record. Requires Full Disk Access to read the real database — tests
pass a fixture via --db.
"""
import hashlib
import sqlite3
from pathlib import Path

import typer

from crm.commands.admin import require_agent
from crm.commands.import_csv import import_app
from crm.config import get_client
from crm.normalize import normalize_email, normalize_phone
from crm.output import err

BATCH = 200
DEFAULT_GLOBS = ["AddressBook-v22.abcddb", "Sources/*/AddressBook-v22.abcddb"]


def _default_db() -> Path | None:
    root = Path.home() / "Library" / "Application Support" / "AddressBook"
    candidates = []
    for g in DEFAULT_GLOBS:
        try:
            candidates += list(root.glob(g))
        except PermissionError:
            return None
    try:
        return max(candidates, key=lambda p: p.stat().st_size) if candidates else None
    except (PermissionError, OSError):
        return None  # TCC can allow glob but block stat — fall through to the FDA hint


def _rows_from(db_path: Path) -> list[dict]:
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        cur = con.cursor()
        people = {pk: {"first": f, "last": l, "org": o} for pk, f, l, o in
                  cur.execute("select Z_PK, ZFIRSTNAME, ZLASTNAME, ZORGANIZATION "
                              "from ZABCDRECORD")}
        emails = cur.execute(
            "select ZOWNER, ZADDRESS from ZABCDEMAILADDRESS where ZADDRESS is not null"
        ).fetchall()
        phones = cur.execute(
            "select ZOWNER, ZFULLNUMBER from ZABCDPHONENUMBER where ZFULLNUMBER is not null"
        ).fetchall()
        con.close()
    except sqlite3.OperationalError as exc:
        err(f"Cannot read {db_path}: {exc}\n"
            "If this is the real AddressBook, grant Full Disk Access to this app: "
            "System Settings → Privacy & Security → Full Disk Access.")
        raise typer.Exit(1)

    out, seen = [], set()
    for owner, kind, value in (
        [(o, "email", v) for o, v in emails] + [(o, "phone", v) for o, v in phones]
    ):
        p = people.get(owner)
        if not p:
            continue
        full_name = " ".join(x for x in ((p["first"] or "").strip(),
                                         (p["last"] or "").strip()) if x)
        if not full_name:
            continue   # a bare number/org is not a person
        norm = normalize_email(value) if kind == "email" else normalize_phone(value)
        if not norm:
            continue
        digest = hashlib.sha256(f"{owner}:{kind}:{norm}".encode()).hexdigest()[:32]
        if digest in seen:   # same number/email stored twice → one staging row
            continue
        seen.add(digest)
        out.append({
            "source": "apple_contacts", "source_external_id": digest,
            "full_name": full_name,
            "email": norm if kind == "email" else None,
            "phone": norm if kind == "phone" else None,
            "company": (p["org"] or "").strip() or None,
            "raw_json": {"abcddb_pk": owner, kind: value},
        })
    return out


@import_app.command("apple-contacts")
def import_apple_contacts(
    db_path: str = typer.Option(None, "--db", help="Path to AddressBook-v22.abcddb "
                                                   "(default: auto-discover)"),
    agent: str = typer.Option("rahul", "--agent"),
):
    client = get_client()
    require_agent(client, agent)
    path = Path(db_path) if db_path else _default_db()
    if not path or not path.exists():
        err("AddressBook database not found or unreadable. Grant Full Disk Access "
            "(System Settings → Privacy & Security) or pass --db <path>.")
        raise typer.Exit(1)
    staged = _rows_from(path)
    for i in range(0, len(staged), BATCH):
        client.table("staging").upsert(
            staged[i:i + BATCH], on_conflict="source,source_external_id").execute()
    typer.echo(f"staged {len(staged)} contact-points from {path}. Next: crm dedup")
