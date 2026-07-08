"""Guard rail: no new direct writes to the contacts table.

Every ``.table("contacts").update(...)`` / ``.insert(...)`` / ``.upsert(...)``
in src/crm must sit at an allowlisted site. Everything else goes through the
survivorship RPCs (so provenance is logged) — see
docs/superpowers/plans/2026-07-07-provenance-write-path-consolidation.md.

Matcher choice: we parse each file with ``ast`` and walk call chains, rather
than grepping the source text. A textual grep — even one that whitespace-
normalizes — is evadable (comments inside a method chain) and can't tell you
*which function* wrote. The AST sees the chain regardless of how it's
formatted, and anchors each hit to its enclosing function, so the allowlist
pins (file, function, kind, count) instead of brittle line numbers.

Known blind spots (stated so the guard isn't over-trusted):
  - two-statement aliasing breaks the chain the walker follows:
    ``t = client.table("contacts"); t.update(...)`` is NOT caught;
  - non-literal table names are NOT caught: ``.table(TABLE)`` or
    ``.table(name="contacts")`` don't match the literal-"contacts" check;
  - ``.delete(`` is not guarded (deletes carry no fact provenance; out of
    the plan's scope);
  - the allowlist keys on function NAME, not qualified path — two same-named
    functions in one file would merge their counts into one entry.
"""
import ast
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src" / "crm"
PLAN = "docs/superpowers/plans/2026-07-07-provenance-write-path-consolidation.md"

# (file relative to src/crm, enclosing function, update|insert|upsert) -> expected count.
# Each entry needs a justification; anything not here fails the guard.
ALLOWLIST = {
    # dedup fill: value written after survivorship-equivalent gating, logged
    # alongside via provenance entries in the same function.
    ("commands/dedup.py", "_fill_and_log", "update"): 1,
    # dedup auto-fold: the batched apply of already-gated fills per cluster.
    ("commands/dedup.py", "_execute_cluster", "update"): 1,
    # dedup create: birth of a canonical contact (birth-logged).
    ("commands/dedup.py", "_create", "insert"): 1,
    # merge/split are documented OUT OF SCOPE for the consolidation —
    # follow-up flagged in the plan; do not copy this pattern elsewhere.
    ("commands/dedup.py", "merge", "update"): 1,
    ("commands/dedup.py", "split", "insert"): 1,
    # enrich run: stamps last_enriched_at — a bookkeeping timestamp,
    # not a fact field, so it carries no provenance.
    ("commands/enrich.py", "run", "update"): 1,
    # contacts add: birth of a contact, birth-logged (Task 3).
    ("commands/contacts.py", "add", "insert"): 1,
    # admin sync_status: bulk connection_status promotion — a workflow status
    # field (like last_enriched_at), not an enriched fact. Found during Task 6
    # reconciliation; NOT in the plan's original survivor list — flagged for
    # plan-owner review rather than silently blessed.
    ("commands/admin.py", "sync_status", "update"): 1,
}

FAIL_HINT = (
    "\n\nDirect writes to contacts bypass provenance. Fix one of two ways:\n"
    "  1. Route the write through the survivorship RPCs (crm enrich apply / "
    "apply_enrichment) so the fact is logged and arbitrated, or\n"
    "  2. If this write genuinely can't carry provenance (bookkeeping "
    "timestamp, contact birth), add provenance logging where possible and an "
    "ALLOWLIST entry in tests/test_write_paths.py with a justification "
    "comment.\n"
    f"Background: {PLAN}"
)


def _chain_touches_contacts(node: ast.Call) -> bool:
    """True if this call's method chain contains .table("contacts")."""
    cur = node.func
    while True:
        if isinstance(cur, ast.Attribute):
            cur = cur.value
        elif isinstance(cur, ast.Call):
            f = cur.func
            if (
                isinstance(f, ast.Attribute)
                and f.attr == "table"
                and len(cur.args) == 1
                and isinstance(cur.args[0], ast.Constant)
                and cur.args[0].value == "contacts"
            ):
                return True
            cur = f
        else:
            return False


def _contact_writes(source: str):
    """Yield (enclosing function name, write kind, lineno) per write."""
    tree = ast.parse(source)
    # map every node to its innermost enclosing function
    parents = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[child] = parent
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in ("update", "insert", "upsert")
            and _chain_touches_contacts(node)
        ):
            fn, cur = "<module>", node
            while cur in parents:
                cur = parents[cur]
                if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    fn = cur.name
                    break
            yield fn, node.func.attr, node.lineno


def test_matcher_defeats_line_break_evasion():
    """A chain split across lines (how the old bulk.py write looked) — and one
    hiding a comment mid-chain, which even whitespace-normalized greps miss —
    must still match."""
    split = 'def f(c):\n    (c.table("contacts")\n     .update({"x": 1})\n     .eq("id", i).execute())\n'
    commented = 'def g(c):\n    (c.table("contacts")  # sneaky\n     .insert({"x": 1}).execute())\n'
    upsert = 'def h(c):\n    (c.table("contacts")\n     .upsert({"x": 1}).execute())\n'
    assert list(_contact_writes(split)) == [("f", "update", 2)]
    assert list(_contact_writes(commented)) == [("g", "insert", 2)]
    assert list(_contact_writes(upsert)) == [("h", "upsert", 2)]


def test_note_is_not_allowlisted():
    """Task 1 removed contacts.py note's direct write; the guard must never
    readmit it silently."""
    assert not any(
        f == "commands/contacts.py" and fn == "note" for (f, fn, _k) in ALLOWLIST
    )


def test_no_unlogged_contact_writers():
    found: dict[tuple, int] = {}
    lines: dict[tuple, list[int]] = {}
    for path in sorted(SRC.rglob("*.py")):
        rel = str(path.relative_to(SRC))
        for fn, kind, lineno in _contact_writes(path.read_text()):
            key = (rel, fn, kind)
            found[key] = found.get(key, 0) + 1
            lines.setdefault(key, []).append(lineno)

    new = {k: v for k, v in found.items() if k not in ALLOWLIST}
    assert not new, (
        "Unallowlisted direct write(s) to the contacts table:\n"
        + "\n".join(
            f"  src/crm/{f} :: {fn}() .{kind}( x{n} (line {lines[(f, fn, kind)]})"
            for (f, fn, kind), n in sorted(new.items())
        )
        + FAIL_HINT
    )

    drift = {
        k: (found[k], ALLOWLIST[k]) for k in ALLOWLIST if found.get(k, 0) != ALLOWLIST[k]
    }
    assert not drift, (
        "Allowlisted site count drift (site removed or duplicated):\n"
        + "\n".join(
            f"  src/crm/{f} :: {fn}() .{kind}( found {got}, allowlist says {want}"
            for (f, fn, kind), (got, want) in sorted(drift.items())
        )
        + "\nIf a site was legitimately removed/moved, update ALLOWLIST to match."
        + FAIL_HINT
    )
