# Network surfacing skill — design

**Date:** 2026-06-14
**Status:** Approved shape, pending spec review

## Goal

Answer one question well: *"Who in my network is a good person for X?"* — where X is an
arbitrary, natural-language criterion (e.g. "CEOs of pre-seed to Series A startups",
"AI infra founders I've met IRL", "people who could intro me to a fintech GP").

The answer is a **ranked list of real people**, each with enough context to act on
immediately: how/where we met, what we last talked about and when, plus **fresh web
intel** on what they're doing right now (current role, funding announcements, news).
It stops at the surfaced people — no message drafting.

## Non-goals

- No message writing, no outreach drafting, no campaigns. The skill brings people up; the
  human takes it from the hook.
- No new app, binary, or service. No change to the CRM's "pure data layer, no sending /
  no web lookups" boundary — the web work lives in the consuming skill, not the CRM.
- Not exhaustive recall. v1 is pragmatic surfacing, not a guarantee that every matching
  person is found (see Limitations).

## Architecture

A single **skill** (an orchestration playbook for Claude Code) that drives the existing
`crm` CLI and web search as tools. Four stages, designed around one principle: **filter on
the cheap, stable signal locally; web-enrich only the survivors** — and that web pass does
double duty (it resolves the volatile part of the filter *and* gathers the news to act on).

```
your question
   │
   ❶ cast a net        local, free   →  crm list/search → candidate ids
   │
   ❷ pull dossiers     local, free   →  crm contact <id> --json (per candidate)
   │
   ❸ live web pass     fresh, bounded → web search survivors: role, stage, news + sources
   │                                    write material findings back to the CRM
   │
   ❹ rank & surface                  →  ranked dossiers, fit-first then warmth
```

### Why this split (the load-bearing reasoning)

The two halves of a query like "CEOs of pre-seed–Series A startups" have **opposite
volatility**:

- *"Is a founder/CEO"* — stable (someone is a founder for years), cheap, already in
  `current_role`. Filter on it locally; it narrows thousands → dozens.
- *"Company is pre-seed–Series A"* — volatile, and it's the **same fact step ❸ web-searches
  anyway** to plan outreach. So storing it buys little; resolve it live on the few survivors.

Filtering live on every contact would mean one web lookup per contact just to know who
qualifies — unworkable at network scale. Storing stage would be a stale snapshot of the one
fact you most want fresh. The hybrid avoids both.

## Components

### A. CRM change 1 — `--role` filter on `crm list` (in-repo)

`crm list` filters by status/tier/tag/affiliation/cold_since but **not role**, so "find
founders" currently means pulling everyone and filtering client-side. Add a `--role`
substring filter (pure read filter — stays inside the data-layer boundary).

- `src/crm/commands/contacts.py` → `list_contacts`: add
  `role: str = typer.Option(None, "--role")`.
- Apply as a case-insensitive substring match on `current_role`, escaping `%`/`_`/`\` in the
  input the same way `_resolve` does, then matching `%{escaped}%` via `.ilike(...)`.
- Composes with the existing filters (AND), like the others.
- No `cli.py` change needed — `list` is registered as the function (`app.command("list")
  (list_contacts)`), so the new option is picked up automatically.

### B. CRM change 2 — make `last_enriched_at` settable (in-repo)

`contacts.last_enriched_at` exists but is absent from `SETTABLE` in `contacts.py`, so the
approved writeback can't stamp it. Add `"last_enriched_at"` to `SETTABLE`. It's a date
field; `set_field` writes the value as-is and logs to `enrichment_log` (Postgres accepts an
ISO date string). This lets the skill stamp "last enriched on YYYY-MM-DD" so repeat asks
know what's fresh. (`current_role` / `current_company` are already settable, so a web-found
job change can be written back too.)

### C. The skill — `surface-network` (out-of-repo: `~/.claude/skills/surface-network/`)

Because `.claude/` is gitignored in this repo, the skill lives in Rahul's global skills dir,
not the repo. The `crm` CLI is already a global install (`uv tool install`), so the skill is
portable. **Open decision below** on whether to also ship a public playbook in the repo.

Skill body (the playbook):

1. **Parse the ask** into two buckets:
   - *Local filters* — role keywords, tags, affiliations, company keywords, status/tier.
   - *Volatile criteria* — funding stage, "currently doing X", recent activity (verified in ❸).
   If the ask is ambiguous (e.g. stage range unstated), ask exactly one clarifying question.

2. **Cast the net** (recall over precision here): run `crm list --json` with whatever local
   filters apply (`--role`, `--status`, `--tier`, `--tag`, `--affiliation`), and/or
   `crm search` for company/notes keywords. Union the candidate ids. If the union exceeds a
   sane cap, keep the warmest and **report what was dropped** — never silently truncate.

3. **Pull dossiers**: `crm contact <id> --json` per candidate → identities, interactions
   (with summaries + dates), `origin_context`, `notes`, `last_touchpoint_*`.

4. **Pre-rank and bound**: order candidates by relationship warmth + obvious fit; take the
   top N (≤ ~40) into the live pass to bound web cost. Report the bound.

5. **Live web pass** (web search/fetch) per survivor: current role/company, funding stage,
   recent news — **with source links**. Apply the volatile filter (drop those who no longer
   fit, e.g. company is now Series C). Where web contradicts the CRM (job change), prefer web.

6. **Writeback** (non-blocking, uses a registered agent id, e.g. `--agent surface`): for each
   survivor with a material finding, `crm note <id> "[enriched 2026-06-14] …"`, stamp
   `crm set <id> last_enriched_at=<today>`, and `crm set` `current_role`/`current_company` if
   they changed. Failures here are logged but never block surfacing.

7. **Surface** the ranked dossiers (see Output).

The skill registers its agent once: `crm agent register surface --desc "network surfacing skill"`.

## Data flow — worked example

> "Who in my network is a CEO of a pre-seed to Series A startup?"

1. Parse → local filter: `current_role` ~ founder/CEO; volatile: stage ∈ {pre-seed … Series A}.
2. `crm list --role founder --status in_network --json` ∪ `crm list --role ceo …` → ~40 ids.
3. `crm contact <id> --json` each → dossiers.
4. Pre-rank by warmth → take all ~40 (under the bound).
5. Web pass: for each, current company + latest round. Drop the 9 now at Series B+. 31 remain.
6. Writeback: note each finding, stamp `last_enriched_at`, fix 3 stale job titles.
7. Surface 31, ranked fit-first then warmth.

## Output format

```
1. Jane Chen — CEO @ Vellum  ·  in_network · t1  ·  last talked 4mo ago
   How you met: SF AI dinner, Mar 2025 (origin)
   Last touchpoint: Apr 2025 — swapped notes on eval tooling
   Fresh: raised $12M Series A led by Benchmark, May 2026 [techcrunch.com/…]
          → hook: her round + your eval-tooling thread
```

Ranked by **fit to the query first, then relationship warmth** (closeness tier + recency),
so a warm t1 who just raised outranks a cold t4 who also fits.

## Error handling & edge cases

- **No candidates**: say so plainly, suggest broadening the net (drop a filter, widen role terms).
- **Ambiguous ask**: one clarifying question, then proceed.
- **Web pass finds nothing** for a person: surface them anyway with relationship context,
  marked "no fresh signal found."
- **Web contradicts CRM**: prefer web for volatile facts; write the correction back.
- **Writeback failure**: log and continue; surfacing must not depend on writes succeeding.
- **Candidate/ survivor caps hit**: always report what was excluded.

## Limitations (stated, not hidden)

- **Recall is bounded by what's stored.** Stage isn't local, so the net can't pre-filter on
  it. A net cast on role text misses people whose `current_role` is vague ("Building
  something new"). The skill will name the likely blind spots for a given query rather than
  imply completeness.
- **Live pass is capped** at the top-N survivors for cost; excluded people are reported.
- **Web intel can be stale or wrong**; sources are always shown so the human can judge.

## Testing

- **CRM `--role`** (`tests/test_contacts.py`): returns only role-matching rows; wildcard
  characters in input are escaped (no injection / no accidental `%`); composes with
  `--status`/`--tier` (AND).
- **`last_enriched_at` settable** (`tests/test_contacts.py`): `set_field` accepts a valid ISO
  date, writes it, and logs to `enrichment_log`. Note it's a `date` column (unlike the
  `text` fields already in `SETTABLE`), so a malformed value is **rejected by Postgres** —
  the test should assert that error surfaces, not that "banana" gets stored.
- **Skill**: not harness-unit-testable. Acceptance check = run the worked example end-to-end
  against the local stack with seeded contacts and confirm the right people surface with
  dossier + intel line and that writeback lands (`crm contact` shows the note +
  `last_enriched_at`).
- Update `README.md`'s `crm list` example to mention `--role`.

## Open decision for spec review

**Skill placement.** Recommend `~/.claude/skills/surface-network/` (global; the `crm` CLI is
already global, so the skill is invokable anywhere). Alternative: also ship a public
`docs/surfacing.md` playbook in the repo to document the canonical consuming-agent pattern
(matches `docs/operational-loads.md`), with the skill as a thin pointer. v1 recommendation:
global skill only; add the public doc later if desired. No PII is involved either way — the
skill is method-only.
