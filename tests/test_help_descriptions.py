"""W3 doc-sweep guardrails: every command's --help opens with a real one-liner,
and none of the flagged descriptions leak internal/history phrasing.

We introspect the Typer app's click command tree directly (no DB needed): each
command's help text is the function docstring / `help=` Typer renders at the top
of `--help`.
"""
import pytest
import typer
from typer.main import get_command

from crm.cli import app

BANNED = [
    "reconnection query",
    "write-isolation",
    "head-count round-trips",
    "additive and idempotent",
]


def _walk(cmd, path=()):
    """Yield (dotted-path, click_command) for every leaf command + group."""
    name = cmd.name or "crm"
    here = path + (name,)
    yield here, cmd
    sub = getattr(cmd, "commands", None)
    if sub:
        for child in sub.values():
            yield from _walk(child, here)


CLICK_APP = get_command(app)
ALL = list(_walk(CLICK_APP))
# leaf commands only for the "has a description" check (groups get their help
# from the sub-app help= string, which we also assert isn't blank)
LEAF = [(p, c) for p, c in ALL if not getattr(c, "commands", None)]


def _help_text(cmd) -> str:
    # click stores the first help line as short_help or derives it from help
    return (cmd.help or cmd.short_help or "").strip()


@pytest.mark.parametrize("path,cmd", ALL, ids=[".".join(p) for p, _ in ALL])
def test_every_command_has_a_description(path, cmd):
    text = _help_text(cmd)
    assert text, f"{'.'.join(path)} has a blank --help description"


@pytest.mark.parametrize("path,cmd", ALL, ids=[".".join(p) for p, _ in ALL])
def test_no_banned_phrases(path, cmd):
    # normalize whitespace so multi-line docstrings can't hide a banned phrase
    # across a newline (click preserves the "\n" between docstring lines)
    text = " ".join(_help_text(cmd).lower().split())
    hit = [b for b in BANNED if b.lower() in text]
    assert not hit, f"{'.'.join(path)} still contains banned phrasing: {hit}"


def test_agent_and_json_help_are_reused_strings():
    """The systemic --agent / --json help must be the single shared phrasing."""
    from crm.output import AGENT_HELP, JSON_HELP

    for path, cmd in LEAF:
        for param in cmd.params:
            opts = getattr(param, "opts", [])
            # assert presence too (help is None == the pre-sweep blank bug), not just drift
            if "--agent" in opts:
                assert param.help == AGENT_HELP, f"{'.'.join(path)} --agent help missing/drifted"
            if "--json" in opts:
                assert param.help == JSON_HELP, f"{'.'.join(path)} --json help missing/drifted"
