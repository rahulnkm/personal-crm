# Plan 2 Completion — Enrichment for Real Searches

**Date:** 2026-06-15 · **Status:** spec, pre-implementation · supersedes the Plan-2 portions of `2026-06-14-crm-enrichment-design.md`

## Goal

Finish enrichment so the CRM answers the owner's real searches: **find people to do business with, find collaborators, get warm intros / "who do I know at X", learn about an industry, share relevant info, reconnect.** `company_category` is already filled network-wide; this completes the rest.

## The compromise: cheap+static across the whole network, best+live only on what a query surfaces

Backed by the investigation: per-contact web research is ~20x more expensive than bulk knowledge-first, and ~30% of role/company data rots yearly — so anything volatile that's bulk-stored is stale before it's used. Therefore:

- **Tier 1 — fill cheap, static signals across all ~2,157 in-network contacts now** (one knowledge-first LLM batch + free extracts + SQL views). This powers *findability/segmentation* — the part that must cover everyone for a search to surface the right people.
- **Tier 2 — reserve live web research for the handful a query surfaces** (current activity, funding, job-change, role-freshness, the outreach brief), cached with a 45-day TTL. This powers *freshness* — only needed for people you're about to act on.

~6 of 8 signals are Tier 1 (cheap, network-wide); only "current activity" and "role freshness" are Tier 2 (live, on-demand).

## Signal → method → which search it serves

| Signal | Tier | Cheapest fill (do now, network-wide) | Best (live, on surfaced people) | Serves |
|---|---|---|---|---|
| **expertise[]** | 1 | knowledge-first LLM batch from role+company+company_category, dedup by company → `expertise[]` | live read of their writing/talks/GitHub | business, collaborate, learn-industry, share |
| **seniority** | 1 | derive from title string (founder/C-level/VP/IC) → `tags[]` | API-verified title | business, intro |
| **origin_context** | 1 | **free**: LinkedIn `Connected On` → "Met via LinkedIn, {date}" (1,429 contacts) | owner manual recall | reconnect, intro framing |
| **interests[]** | 1 | mine existing `notes` + interaction topics (bounded by what's logged) | live read of public posts | share, reconnect |
| **who-do-I-know-at-X** | 1 | **free SQL**: co-affiliation `GROUP BY current_company`/`affiliations` | — (multi-hop needs data we lack) | warm intro |
| **going-cold** | 1 | **free SQL view**: days-since-touch, threshold tiered by closeness_tier | auto-ingest new touchpoints | reconnect |
| **current activity / news / funding / job-change** | 2 | — (don't bulk-store; it rots) | live web at query time, cache 45d | business, reconnect |
| **current_role freshness** | 2 | (have stale value) | live web verify on surface | business, intro |
| location, avatar, socials | — | **not free** — LinkedIn export omits them; low value for these searches → defer/JIT only | live web | (minor) |

Honest cuts (data-limited, confirmed): LinkedIn import carries only name/title/company/connected-on — **no location, socials, avatar, or headline**, so those aren't bulk-fillable cheaply and don't power the core searches; skip or JIT. **Multi-hop warm-intro paths are not feasible** — the schema holds only owner↔contact edges, no contact↔contact graph. "Who do I know at X" = first-degree only.

## Engine work to enable it (build order)

1. **Array write-path (M) — the #1 blocker.** `expertise`/`interests` are `text[]`; the survivorship RPC returns `noop` for them and `crm set` only handles tags/affiliations, so today every array candidate is silently dropped — `expertise` can't be filled at all. Add an `enrich_apply_array` RPC: advisory-locked, set-union (`array_append` guarded by `= any()`), idempotent, per-element provenance (no `is_current`), tombstone-aware; a recompute sibling for reject. Route array fields to it in `apply`/`run`; fold `crm set` arrays in; add `expertise`/`interests` to `SETTABLE`. *(new migration, enrich.py, commands/enrich.py, commands/contacts.py)*
2. **`crm enrich run` fixes (S).** `--limit` counts only enriched contacts (move the counter above the skip `continue`s) → make it count all touched; add `--all`/clear-status for true bulk (today defaults to in_network only); tally `review` outcomes so review-only contacts aren't mislabeled `no_signal`, and add `reviewed` to the summary. *(commands/enrich.py)*
3. **Freshness wiring (M).** `refresh_after` is read in 3 places but **never written** — the freshness clock is dead. Add `p_refresh_after` to the RPC INSERT + a field→TTL map (volatile role/company/category 45d, stable 180–365d, email_status 180d, manual=NULL). Add `crm enrich due` (contacts with a stale `is_current` row, closeness-ordered) feeding `crm enrich run --due`. *(new migration, commands/enrich.py)*
4. **Durable research-agent skill (M).** Replace the ad-hoc fill subagents with a documented Claude Code skill (no LLM code in the package). Loop: gaps (from `crm contact --json` provenance/stale) → presence gate → **two calls** (Sonnet research with `web_search`+citations, then Haiku structured extract — they can't be one call: web_search forces citations which are incompatible with forced structured output) → deterministic confidence (citation-support + n_sources, never LLM self-report) → write via `crm enrich apply` (scalars→survivorship RPC, expertise→array RPC). Bulk mode (the `no_signal` residual from `enrich run`) + JIT mode (the ~40 a `crm find` surfaces). Pairs with the existing `surface-network` skill. *(docs/agents/enrich-agent.md)*
5. **Relationship graph — first-degree (L, Phase 3, optional).** New `relationships(contact_id, related_contact_id, type, source)` table + `birthday_*`/`cadence_days` columns; `crm who-knows <company>` (first-degree, closeness-ranked) + `crm relate`. Edges from shared-company inference + agent-found public bios (labeled inference per the §6.4 boundary). No multi-hop path scoring (data can't support it).

## Cheap bulk pass (the Tier-1 fill, once item 1 lands)

Same proven pipeline as the `company_category` fill: free `origin_context` copy (1,429) → knowledge-first `expertise`+`seniority` batch (4 subagents, dedup by company, no web) → bulk SQL write through the array RPC → web-residual only for the obscure. Plus two free SQL views (who-do-I-know-at-X, going-cold). Expected: minutes, near-zero cost, network-wide.

## Verify before building
- Current Claude model IDs + pricing via the `claude-api` skill (the older spec's "Sonnet 4.6 / Haiku 4.5" need reconfirming).
- The `web_search` + structured-output 400 with a 10-line script (the two-call design hinges on it).

## Recommended order
1 → 2 → 3 (engine correct + freshness) → Tier-1 cheap bulk pass → 4 (durable agent for JIT) → 5 (graph, optional). Items 1–3 + the bulk pass deliver the searchable network; 4 delivers fresh briefs on demand; 5 is the warm-intro layer.
