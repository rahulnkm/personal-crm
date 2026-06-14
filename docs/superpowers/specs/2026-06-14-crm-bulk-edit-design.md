# CRM CLI — Bulk-edit commands (`crm bulk <verb>`) — Design

**Date:** 2026-06-14
**Status:** Approved for planning
**Companion spec:** `2026-06-14-crm-perf-fixes-design.md` (shares migration `0006`)

## Problem

Every mutation in the CLI is single-record only. The only set-aware writes are
`sync-status` (one-way promote) and `event add` (one event, many participants).
For an agent-driven CRM whose whole job is acting on **segments** ("everyone I
haven't talked to in 6 months", "tag all YC founders"), the only way to edit a
cohort today is a shell `for` loop — which multiplies the N+1 pattern across every
mutation helper. This spec adds a `crm bulk <verb>` namespace for cohort
operations, each issuing set-based writes.

## CLI surface

A new Typer sub-app `bulk` with four verbs. Cohort is selected with the **same
filter flags as `crm list`** (decision: reuse, don't invent grammar):
`--status`, `--tier`, `--tag`, `--affiliation`, `--cold-since`. Filters compose
with AND; an empty filter set is rejected (refuse to act on "everyone" by
accident — must pass `--all` to mean all).

Every verb supports:
- `--dry-run` — resolve the cohort, print the count + a sample (first ~10
  full_names), write nothing. Exit 0.
- confirmation gate — if writing and stdout is a TTY and neither `--yes` nor
  `--json` is set, prompt `Apply <verb> to N contacts? [y/N]`. `--yes` skips the
  prompt; `--json` implies non-interactive (agents) and skips the prompt.
- `--json` — emit `{"affected": [<ids>], "count": N}` instead of the human line.
- `--agent <id>` — validated **once** via `require_agent` (never per row).

```
crm bulk set <field>=<value>   [filters] [--dry-run] [--yes] [--json] [--agent]
crm bulk tag <tag>             [filters] [--dry-run] [--yes] [--json] [--agent]
crm bulk log  --kind <k> [--channel --date --summary] [filters] [--dry-run] [--yes] [--json] [--agent]
crm bulk note <text>           [filters] [--dry-run] [--yes] [--json] [--agent]
```

## Shared infra

### `_resolve_cohort(client, filters) -> list[str]`
Factor the filter-building block out of `list_contacts` (`contacts.py:88–103`)
into one function returning contact ids. Both `list` and every bulk verb call it,
so the filter semantics are defined once and tested once.

- Builds the same query (`status`→`connection_status`, `tier`→`closeness_tier`,
  `tag`→`.contains("tags",[tag])`, `affiliation`→`.contains("affiliations",…)`,
  `cold_since`→ the `or_` last_touchpoint cutoff).
- **Paginates past 1000** with `.range()` until drained (bulk ops must not
  silently truncate at the PostgREST cap — unlike `list`, which intentionally
  shows a top slice). Returns all matching ids.
- `list_contacts` is refactored to build its display query from the same filter
  spec (keep its `--limit`/ordering behavior; no user-visible change).

Location: a small new module `src/crm/bulk.py` holding `_resolve_cohort`, the
shared confirm/dry-run helper, and the chunked-RPC caller. Commands live in
`src/crm/commands/bulk.py`.

## The four verbs

### `crm bulk set <field>=<value>`
- **Scalar fields only.** Validate `field ∈ SETTABLE` AND `field ∉ ARRAY_FIELDS`;
  reject array fields (`tags`, `affiliations`) with a usage error
  (`"bulk set handles scalar fields; for tags use: crm bulk tag <tag> …"`),
  exit 2. This keeps `bulk set` to a single clean code path and avoids a second
  array-append RPC — `tags` already has the dedicated `bulk tag` verb, and bulk
  `affiliations` append is explicitly deferred (YAGNI; add a `crm bulk affiliate`
  verb later if needed).
- If the field is an enum, validate `value ∈ ENUM_VALUES[field]` — reuse
  `contacts.py` constants.
- Scalar field → **same value across the set**, so one
  `client.table("contacts").update({field:value,
  "updated_at":"now()"}).in_("id", ids).execute()` (one round-trip, no RPC).
- One batched `enrichment_log.insert([...])` (method `'bulk_set'`, source=agent),
  chunked ≤ 500.
- Covers the **bulk status change** case (set `connection_status`) and
  **bulk closeness override** (set `closeness_tier`) the audit called out as
  missing.

### `crm bulk tag <tag>`
- Registry-check the tag once (`tag_registry`), as the single `set` does.
- RPC **`bulk_add_tag(p_tag text, p_ids uuid[]) returns int`**:
  `update contacts set tags = array_append(tags, p_tag), updated_at = now()
   where id = any(p_ids) and not (tags @> array[p_tag])` — idempotent (no dupes),
  one statement. Returns affected count.
- Chunk `p_ids` ≤ 500.

### `crm bulk log`
- Same touchpoint logged against the whole cohort. Validate `kind ∈ VALID_KINDS`,
  validate date.
- One `interactions.insert([...])` for all contacts (chunked ≤ 500) + one bulk
  last-touchpoint bump using the shared `_bump_last_touchpoint_bulk` helper from
  the perf spec (one `.in_()` read + one `.update().in_(ids_to_bump)`). The bump
  writes `last_touchpoint_topic = summary` (mirrors single `crm log`, which passes
  `summary` as the topic), `last_touchpoint_channel = channel`.
- This is the "sent the newsletter / hosted a dinner" cohort touchpoint.

### `crm bulk note <text>`
- Per-row read-modify (append to each contact's existing `notes`), so it needs an
  RPC. **`bulk_append_note(payload jsonb) returns int`** where payload is
  `[{id, stamped}]` (Python builds the `[date agent] text` stamp per row, same
  format as single `note`): `update contacts c set notes =
  case when c.notes is null or c.notes = '' then p.stamped
       else c.notes || E'\n' || p.stamped end,
  updated_at = now() from jsonb_to_recordset(payload) as p(id uuid, stamped text)
  where c.id = p.id`. Chunk ≤ 500.

## New SQL — migration `0006_bulk_operations.sql`
Functions added by THIS spec: `bulk_add_tag(text, uuid[])`,
`bulk_append_note(jsonb)`. (The perf spec adds `attach_and_fill`,
`bulk_upsert_interactions`, `crm_stats` to the same migration.) All:
`set search_path = public`, `grant execute … to service_role`.

## Safety & semantics
- **No empty-filter mass writes:** at least one filter or explicit `--all`.
- **Dry-run is the default mental model:** docs/help lead with `--dry-run`.
- **Idempotent where natural:** `bulk tag` skips contacts already carrying the
  tag; `bulk set` is naturally idempotent.
- **Atomicity:** each RPC is one statement (atomic). The non-RPC `bulk set`
  (update + enrichment_log insert) is two calls; on failure of the log insert the
  update has already applied — acceptable (enrichment_log is an audit trail, not a
  gate), documented in the command docstring. This is **not a regression**:
  single `set_field` already orders update-then-log the same way.
- Exit codes follow the project: 0 ok, 1 error, 2 usage.

## Testing
- Local stack only. For each verb: dry-run writes nothing; confirm gate respected
  (`--yes`/`--json` skip; TTY prompt path tested via monkeypatch); `--json` shape;
  agent-validated-once (assert `require_agent`/agents-update called once, not per
  row); empty-filter refusal; `--all` path.
- `_resolve_cohort`: each filter, AND composition, pagination past 1000 (seed
  >1000 rows, assert all returned).
- RPC correctness: `bulk_add_tag` idempotency + count; `bulk_append_note` append
  vs first-note; chunking boundary (exactly 500 / 501 rows).
- Round-trip regression: bulk verbs issue O(1) writes per chunk, asserted by
  spying on the client.

## Success criteria
- Four `crm bulk` verbs, cohort via list filters, dry-run + confirm + json + agent.
- 100% line coverage on new/changed code.
- `scripts/bench_bulk.py` demonstrates the bulk path vs an equivalent per-row loop
  on a seeded N: round-trip count and wall-clock, median/p90.
- README "The loop" section updated with the bulk verbs.
