"""W4: `crm review` (identity-match queue) vs `crm enrich review` (field values).

Canonical is `crm match review`; flat `crm review` stays as a deprecated alias
that prints a disambiguation hint to STDERR (so --json on stdout stays clean),
then runs the identical handler. These tests assert the wiring + hint routing
without a DB — `_review_impl` is mocked so both entry points are exercised
purely through Typer's command tree.
"""
from unittest.mock import patch

from typer.testing import CliRunner

from crm.cli import app

# Click 8.2+ keeps result.stdout / result.stderr separate by default, so we can
# assert the hint lands on stderr, not stdout.
runner = CliRunner()


def test_both_entry_points_call_the_same_impl():
    """`crm match review` and `crm review` both delegate to _review_impl with
    identical args — proving they can't diverge in behavior."""
    with patch("crm.commands.dedup._review_impl") as impl:
        r1 = runner.invoke(app, ["match", "review", "--json"])
    assert r1.exit_code == 0, r1.output
    assert impl.call_count == 1
    canonical_args = impl.call_args

    with patch("crm.commands.dedup._review_impl") as impl:
        r2 = runner.invoke(app, ["review", "--json"])
    assert r2.exit_code == 0, r2.output
    assert impl.call_count == 1
    alias_args = impl.call_args

    # Same positional call signature (approve, reject, to, as_json, agent).
    assert canonical_args == alias_args


def test_alias_emits_hint_on_stderr():
    with patch("crm.commands.dedup._review_impl"):
        r = runner.invoke(app, ["review", "--json"])
    assert r.exit_code == 0
    assert "crm match review" in r.stderr
    assert "crm enrich review" in r.stderr
    # The hint must NOT leak onto stdout (would corrupt --json consumers).
    assert "note:" not in r.stdout


def test_canonical_does_not_emit_hint():
    with patch("crm.commands.dedup._review_impl"):
        r = runner.invoke(app, ["match", "review", "--json"])
    assert r.exit_code == 0
    assert "note:" not in r.stderr
    assert "crm enrich review" not in r.stderr


def test_dedup_help_still_works_and_is_the_action():
    """`crm dedup` must remain a flat action command, not a group."""
    r = runner.invoke(app, ["dedup", "--help"])
    assert r.exit_code == 0
    assert "--workers" in r.stdout  # still the action's own option
    # A group would list subcommands under "Commands:"; the action must not.
    assert "review" not in r.stdout.lower().split("options")[0]


def test_match_review_help_has_description():
    r = runner.invoke(app, ["match", "review", "--help"])
    assert r.exit_code == 0
    assert "identity-match queue" in r.stdout
