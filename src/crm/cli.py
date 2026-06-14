"""Typer application entry point. Wires command sub-apps; exposes --version."""
import sys

import typer

from crm import __version__
from crm.commands.admin import agent_app, stats, sync_status, tags_app
from crm.commands.contacts import add, contact, list_contacts, note, search, set_field
from crm.commands.backfill import backfill
from crm.commands.dedup import dedup, merge, review, split
from crm.commands.enrich import enrich_app
from crm.commands.import_csv import import_app
import crm.commands.import_touchpoints  # noqa: F401  (registers import subcommand)
import crm.commands.import_linkedin  # noqa: F401  (registers import subcommand)
import crm.commands.import_apple  # noqa: F401  (registers import subcommand)
import crm.commands.import_imessage  # noqa: F401  (registers import subcommand)
from crm.commands.log import event_app, log
from crm.output import err

app = typer.Typer(help="Personal CRM — Rahul's real-network base. Pure data layer.")

app.add_typer(agent_app, name="agent")
app.add_typer(tags_app, name="tags")
app.add_typer(import_app, name="import")
app.add_typer(enrich_app, name="enrich")
app.command("stats")(stats)
app.command("sync-status")(sync_status)
app.command("dedup")(dedup)
app.command("review")(review)
app.command("merge")(merge)
app.command("split")(split)
app.command("contact")(contact)
app.command("list")(list_contacts)
app.command("search")(search)
app.command("add")(add)
app.command("set")(set_field)
app.command("note")(note)
app.command("log")(log)
app.command("backfill")(backfill)
app.add_typer(event_app, name="event")


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"crm {__version__}")
        raise typer.Exit()


@app.callback()
def _version_root(
    version: bool = typer.Option(
        False, "--version", callback=_version_callback, is_eager=True,
        help="Show version and exit.",
    ),
):
    """Personal CRM data layer. All writes are attributed via --agent."""


def main() -> None:
    """Entry point: converts uncaught DB/API errors into one-line stderr + exit 1."""
    try:
        app()
    except SystemExit:
        raise
    except Exception as exc:  # postgrest APIError, httpx errors, etc.
        err(f"error: {exc}")
        sys.exit(1)
