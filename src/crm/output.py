"""stdout discipline: data on stdout (JSON or table), everything else stderr."""
import json
import sys

import typer

# Shared --option help strings, reused across every command so the phrasing is
# identical everywhere.
AGENT_HELP = "Registered writing agent for audit attribution"
JSON_HELP = "Emit JSON instead of a table"


def render(rows: list[dict], as_json: bool) -> None:
    if as_json:
        typer.echo(json.dumps(rows, default=str))
        return
    if not rows:
        typer.echo("0 rows")
        return
    def cell(r: dict, c: str) -> str:
        v = r.get(c)
        return "" if v is None else str(v)  # 0 and False must still display

    # rows come from one table query → homogeneous keys; first row defines columns
    cols = list(rows[0].keys())
    widths = {c: max(len(c), *(len(cell(r, c)) for r in rows)) for c in cols}
    typer.echo("  ".join(c.ljust(widths[c]) for c in cols))
    for r in rows:
        typer.echo("  ".join(cell(r, c).ljust(widths[c]) for c in cols))


def err(msg: str) -> None:
    print(msg, file=sys.stderr)
