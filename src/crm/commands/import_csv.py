"""Generic CSV → staging importer.

--map binds staging fields to CSV headers: "full_name=Name,email=Email,company=Firm".
Mappable fields: full_name, email, phone, linkedin_url, handle, role, company, location.
Rows are upserted on (source, source_external_id) where the external id is a
row-content hash — re-running an import (wifi drop, crash) creates no duplicates.
"""
import csv
import hashlib
import json

import typer

from crm.commands.admin import require_agent
from crm.config import get_client
from crm.normalize import normalize_email, normalize_linkedin, normalize_phone
from crm.output import err

import_app = typer.Typer(help="Importers. Everything lands in staging; run `crm dedup` after.")

MAPPABLE = {"full_name", "email", "phone", "linkedin_url", "handle", "role", "company", "location",
            "first_name", "last_name"}
BATCH = 200


def _read_rows(path: str) -> tuple[list[str], list[dict]]:
    for encoding in ("utf-8-sig", "latin-1"):
        try:
            with open(path, newline="", encoding=encoding) as f:
                reader = csv.DictReader(f)
                headers = reader.fieldnames or []
                rows = list(reader)
            if encoding != "utf-8-sig":
                err(f"note: {path} is not UTF-8; read as {encoding}")
            return headers, rows
        except UnicodeDecodeError:
            continue
        except FileNotFoundError:
            err(f"File not found: {path}")
            raise typer.Exit(1)
    err(f"Could not decode {path} as utf-8 or latin-1")
    raise typer.Exit(1)


def _parse_map(map_str: str) -> dict[str, str]:
    pairs = [p.split("=", 1) for p in map_str.split(",") if p]
    bad = [p for p in pairs if len(p) != 2]
    if bad:
        err(f"Malformed --map entries (need field=Header): {['='.join(p) for p in bad]}")
        raise typer.Exit(1)
    mapping = {k.strip(): v.strip() for k, v in pairs}
    unknown = set(mapping) - MAPPABLE
    if unknown:
        err(f"Unknown staging fields in --map: {sorted(unknown)}. Allowed: {sorted(MAPPABLE)}")
        raise typer.Exit(1)
    return mapping


@import_app.command("csv")
def import_csv(
    file: str = typer.Argument(..., help="Path to the CSV"),
    source: str = typer.Option(..., "--source", help="Source slug, e.g. csv_rutgers_vc"),
    map_str: str = typer.Option(..., "--map", help="field=Header,field=Header"),
    agent: str = typer.Option("rahul", "--agent"),
):
    client = get_client()
    require_agent(client, agent)
    mapping = _parse_map(map_str)

    headers, rows = _read_rows(file)
    missing = [h for h in mapping.values() if h not in headers]
    if missing:
        err(f"CSV is missing mapped headers: {missing}. Headers found: {headers}")
        raise typer.Exit(1)

    staged, skipped = [], 0
    seen = set()  # dedupe within the file to avoid ON CONFLICT double-update error
    for raw in rows:
        rec = {field: (raw.get(header) or "").strip() or None
               for field, header in mapping.items()}
        # compose full_name from first_name / last_name if not already present
        if not rec.get("full_name") and (rec.get("first_name") or rec.get("last_name")):
            rec["full_name"] = " ".join(
                x for x in (rec.get("first_name"), rec.get("last_name")) if x
            )
        rec.pop("first_name", None)
        rec.pop("last_name", None)
        if not rec.get("full_name"):
            skipped += 1
            continue
        rec["email"] = normalize_email(rec.get("email"))
        rec["phone"] = normalize_phone(rec.get("phone"))
        rec["linkedin_url"] = normalize_linkedin(rec.get("linkedin_url"))
        digest = hashlib.sha256(
            json.dumps(raw, sort_keys=True).encode()
        ).hexdigest()[:32]  # hash covers all raw columns — if the CSV gains new columns on re-export, rows re-stage
        dedup_key = (source, digest)
        if dedup_key in seen:
            skipped += 1
            continue
        seen.add(dedup_key)
        staged.append({**rec, "source": source, "source_external_id": digest,
                       "raw_json": raw})

    for i in range(0, len(staged), BATCH):  # batched: flaky-wifi loses ≤1 batch, rerun is safe
        client.table("staging").upsert(
            staged[i : i + BATCH], on_conflict="source,source_external_id"
        ).execute()

    typer.echo(f"staged {len(staged)} rows from {file} as source={source} "
               f"(skipped {skipped} nameless). Next: crm dedup")
