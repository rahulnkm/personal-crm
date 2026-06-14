# CRM CLI — Bulk-edit commands (`crm bulk <verb>`) — Design

**Date:** 2026-06-14
**Status:** Approved for planning (revised after 2nd adversarial review)
**Companion spec:** `2026-06-14-crm-perf-fixes-design.md`
**Sequencing:** implement AFTER the perf spec — it consumes the
`_bump_last_touchpoint_bulk` helper that the perf spec creates. This spec adds its
own migration `0009_bulk_edit_rpcs.sql` (separate file → no co-edit conflict with
perf's `0006`).

## Problem

Every mutation in the CLI is single-record only (the only set-aware writes are
`sync-status` and `event add`). For an agent-driven CRM whose job is acting on
**segments**, the only way to edit a cohort today is a shell `for` loop —
multiplying the N+1 pattern across every mutation helper. This spec adds a
`crm bulk <verb>` namespace for cohort operations issuing set-based writes.

**Verbs (revised):** `set`, `tag`, `log`. (`note` is **deferred to v-next** — the
least-likely operation backed by the most complex write; tags + `enrichment_log`
already cover cohort provenance. Clean additive verb later.)

**Threat model:** the safety guards below target the **agent**, not the human. An
LLM resolving "everyone" to an empty filter and silently rewriting the whole table
is the real failure mode the guards prevent.

## CLI surface

A new Typer sub-app `bulk`, registered like the existing sub-apps
(`app.add_typer(bulk_app, name="bulk")`). Cohort selected with the **same filter
flags as `crm list`**: `--status`, `--tier`, `--tag`, `--affiliation`,
`--cold-since` (compose with AND).

```
crm bulk set <field>=<value>   [filters] [--all] [--dry-run] [--yes] [--json] [--agent]
crm bulk tag <tag>             [filters] [--all] [--dry-run] [--yes] [--json] [--agent]
crm bulk log  --kind <k> [--channel --date --summary] [filters] [--all] [--dry-run] [--yes] [--json] [--agent]
```

### Shared flag semantics (specified exactly — each is a tested branch)
**No interactive prompt** (decided after review — no existing command prompts;
`sync-status`, the set-based analog, just uses `--dry-run`. A TTY prompt would be
a lone UX divergence and an agent footgun). Instead: **a write requires `--yes`**;
`--dry-run` previews; `--json` is purely an output format (orthogonal to both).

- **Filters / `--all`:** at least one filter OR `--all` is required; **empty
  filters without `--all` → exit 2** ("refusing to act on all contacts; pass a
  filter or --all"). **`--all` together with any filter → exit 2**
  ("--all cannot be combined with filters").
- **Write requires `--yes`:** a real write without `--yes` and without `--dry-run`
  → exit 2 ("pass --dry-run to preview or --yes to apply"). This is the single
  safety gate (no TTY detection, no `typer.confirm`).
- **`--dry-run`:** resolve the cohort, print count + sample (first ~10 full_names,
  with id shown so a blank/whitespace name is still legible); writes nothing,
  exit 0. Ignores `--yes`/`--agent` (read-only; agent not required for preview).
- **Empty cohort (0 matches):** issue **no write / no RPC**, exit 0.
- **`--json`** (works with dry-run AND real): uniform shape, one `affected` key +
  a `dry_run` discriminator so agents never branch on key name:
  - dry-run: `{"dry_run": true, "cohort_count": N, "affected": [<ids>]}` (`affected` = the cohort that *would* be acted on).
  - real: `{"dry_run": false, "cohort_count": N, "affected": [<ids>], "changed_count": M}` where `affected` = ids actually changed (= cohort for `set`/`log`; the newly-tagged subset for `tag`), `changed_count = len(affected)`.
  - empty cohort: `{"dry_run": false, "cohort_count": 0, "affected": [], "changed_count": 0}`.
- **`--agent`:** validated **once** via `require_agent`, by the shared gate,
  **after** flag/`--all` validation and **before** cohort resolution and any
  write; **skipped on dry-run**. Verbs do not call `require_agent` themselves.
- **Cohort ids are de-duplicated** (distinct) before any chunked write.

## Shared infra (`src/crm/bulk.py`)
- **`_resolve_cohort(client, filters) -> list[str]`** — factor the filter block out
  of `list_contacts` (contacts.py:89–103); both `list` and bulk verbs call it.
  Same mappings (`status→connection_status`, `tier→closeness_tier`,
  `tag→.contains("tags",[tag])`, `affiliation→.contains("affiliations",…)`,
  `cold_since→` the `or_` cutoff). **Paginates past 1000** with `.range()` until
  drained (bulk must not silently truncate), returns **distinct** ids.
  `list_contacts` is refactored to build from the same filter spec (keeps its
  `--limit`/ordering; no user-visible change). PAGE is a monkeypatchable constant
  (independent of `backfill.PAGE`). **Boundary: the shared helper applies ONLY the
  five filter clauses (`.eq`/`.contains`/the int-derived `cold_since` `.or_`) to a
  passed-in query builder and returns it — it must NOT own `.select()` columns,
  `.order()`, `.limit()`, or `.range()`.** `_resolve_cohort` selects `id`, applies
  the helper, paginates with its own `.range()`, returns `sorted(set(ids))`;
  `list_contacts` selects display columns, applies the helper, adds its own
  `.order(nullsfirst)/.limit`. No user free-text reaches `.or_()` (the only `.or_`
  is the int-derived date cutoff — injection-safe; any future free-text cohort
  filter MUST use the `search`-style sanitizer).
- Shared `_gate` (validate `--all`/filters → `require_agent` (skip on dry-run) →
  resolve → dry-run/empty short-circuit → `--yes` write check) and `_emit`
  (unified JSON) helper, plus a chunked-write helper (`CHUNK = 500`,
  monkeypatchable for fast boundary tests). No interactive prompt.

## The three verbs

### `crm bulk set <field>=<value>`
- Parse `field=value`; **no `=` → exit 2** (matches single `set_field`).
- **Scalar fields only:** `field ∈ SETTABLE` AND `field ∉ ARRAY_FIELDS`. A
  non-settable field → exit 1 (matches single `set_field`); an **array field**
  (`tags`/`affiliations`) → exit 2 with `"bulk set handles scalar fields; for tags
  use: crm bulk tag <tag>"`. (The array-field→exit-2 is an **intentional
  divergence** from single `set_field`, which appends to arrays — bulk treats it as
  a usage error. Bulk `affiliations` append is deferred — YAGNI.)
- Enum field: `value ∈ ENUM_VALUES[field]` else exit 1 (matches single `set`).
- Write, **ONE loop per chunk over the same id slice** (update + log stay aligned;
  a crash leaves a consistent prefix). Per chunk: (1) **read** the slice's current
  `{id: <field value>}` via one `.in_("id", chunk)` select — captures real per-row
  `old_value` so the audit trail is reversible (this is the most dangerous verb, so
  it gets a real `old_value`, not a None placeholder); (2)
  `update({field:value,"updated_at":"now()"}).in_("id", chunk)`; (3) one
  `enrichment_log.insert([...])` with `method='bulk_set'`, `source=agent`,
  `old_value` = captured value. All three chunked ≤ CHUNK (a 500+ id `.in_()` blows
  the URL length).
- Covers **bulk status change** (`connection_status`) and **bulk closeness
  override** (`closeness_tier`) — the named gaps.
- Atomicity note (not a regression): not atomic across chunks (crash → consistent
  prefix applied); `bulk set` is idempotent on rerun (same value) so it self-heals;
  same update-then-log ordering as single `set_field`.

### `crm bulk tag <tag>`
- Registry-check the tag once (`tag_registry`), as single `set` does; unknown → exit 1.
- RPC **`bulk_add_tag(p_tag text, p_ids uuid[]) returns setof uuid`**:
  ```
  update contacts
  set tags = (select array_agg(t order by t)
              from unnest(array_append(tags, p_tag)) t),
      updated_at = now()
  where id = any(p_ids) and not (tags @> array[p_tag])
  returning id;
  ```
  - **Idempotent** (`@>` guard skips contacts already carrying the tag).
  - **Sorted** array (matches single `set_field`'s `sorted(set(...))`, avoids a
    divergent stored representation between the two paths).
  - **Returns the affected ids** so `--json` `affected:[ids]` is accurate and the
    human count = `len(returned)` (the cohort may be larger than affected when some
    already had the tag). Concurrency-safe: single-statement read-modify-write
    under the row lock, no lost update.
- `p_ids` chunked ≤ CHUNK.

### `crm bulk log`
- Same touchpoint against the whole cohort. Validate `kind ∈ VALID_KINDS` (exit 1),
  `--date` via `_validate_iso_date` (exit 1).
- One `interactions.insert([...])` per chunk + the bump via the perf spec's
  `_bump_last_touchpoint_bulk(client, ids, occurred, channel, topic=summary)` —
  now RPC-backed (`bulk_bump_last_touchpoint`), so equal-date no-op, None-date
  skip, and the lost-update race are all handled server-side.
  `last_touchpoint_topic = summary` mirrors single `crm log`.

## New SQL — migration `0009_bulk_edit_rpcs.sql`
Function: `bulk_add_tag(p_tag text, p_ids uuid[]) returns setof uuid`.
`set search_path = public, extensions`, then `revoke execute … from public` AND
`grant execute … to service_role`, with a `drop function if exists
bulk_add_tag(text, uuid[])` rollback line in the PR. `contacts.tags` is
`NOT NULL default '{}'` so `array_append`/`@>` never hit NULL (verified). **No new
index** — cohort filters hit existing `contacts_tags_gin` /
`contacts_affiliations_gin` and small enums on a ~thousand-row table.

## Testing
Local stack only; same infra as the perf spec (`pytest-cov` + `diff-cover` vs
`main`; migrations `0006`/`0007`/`0008` applied via `supabase db reset` preflight; behavioral DB
tests for the RPC since plpgsql is invisible to coverage; the `get_client`-factory
counting proxy from `tests/_spy.py`; monkeypatchable `PAGE`/`CHUNK`; bulk-seed big
fixtures in one insert).

Per-verb / shared-flag cases (each a pinned assertion):
- `_resolve_cohort`: each filter, AND composition, **distinct** output,
  pagination past PAGE (monkeypatched PAGE=2, seed 3 → both loop branches).
- Flag matrix: empty-filter-no-`--all` → exit 2; `--all`+filter → exit 2; empty
  cohort → no RPC, exit 0, `count:0`, no prompt; `--dry-run` shape; `--dry-run
  --json` (`would_affect`); non-TTY-no-flags → exit 2; TTY confirm y / N (mock
  `typer.confirm`); `--json` shape; agent-not-registered → exit 1 (and validated
  before resolution; not required in dry-run); agent-validated-once (spy counts the
  `agents` select call, not per row).
- `bulk set`: no-`=` → exit 2; non-settable → exit 1; array field → exit 2; bad
  enum → exit 1; update + log chunked at boundary (CHUNK=2, 3 rows → 2 chunks);
  `enrichment_log` rows written.
- `bulk tag`: idempotency (cohort with some already-tagged → returns only newly
  affected ids; count reflects affected, not cohort); sorted array; boundary chunk.
- `bulk log`: multi-contact insert; bump edge cases inherited from the shared
  helper (equal-date, None-date, empty ids); `topic=summary`.

## Success criteria
- Three `crm bulk` verbs (`set`/`tag`/`log`), cohort via list filters, with the
  full flag matrix above behaving as specified.
- 100% line coverage on new/changed code via `diff-cover`; RPC covered by
  behavioral DB tests.
- README "The loop" section updated with the bulk verbs and the `--dry-run`-first
  workflow.
- Round-trip wins shown by `scripts/bench_bulk.py` (shared with perf spec):
  bulk verb vs the reference per-row loop — call-count primary, remote-equivalent
  wall-clock labeled.
