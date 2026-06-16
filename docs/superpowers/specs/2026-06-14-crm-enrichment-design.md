# Network Retrieval & Enrichment for the `crm` CLI — Design Spec

**Date:** 2026-06-14
**Status:** Approved design (v2, post 8-way adversarial review), pre-implementation
**Author:** Rahul + Claude (brainstormed)

## 1. Summary

Make the `personal-crm` `crm` CLI (Python over Postgres/Supabase) answer
plain-language questions about your real-world network and act on the answers:

> **Ask your network a question → it surfaces the right people with full context
> → refreshes what they're up to now → drafts your move.**

The interface is **Claude Code driving the CLI** (no MCP, no LLM SDK in the data
layer). Enrichment exists to make people **findable**, not to decorate records.

Driving use cases (the spec is judged against these):
- "Find CEOs of pre-seed–Series A startups in my network I haven't talked to in 3
  months, see what they're up to now, help me reach out." (structured filter)
- "Who can talk to me about entering VC?" → investors / VC-firm operators in the
  network. (semantic match on enriched role/company-category=VC + expertise)
- "Who could teach me about cybersecurity?" → the CTO of a security company.
  (semantic match on *enriched* company-category signal)
- "Who could help me cook for a party in NYC?" → a friend who's a trained chef.
  (semantic match on *your private* notes/tags signal)
- "Who in LA could connect me to a celebrity they know?" → well-connected
  entertainment-adjacent people in LA. (structured location + semantic connector
  signal; see §6.4 on the second-degree boundary.)

**Goal posture (owner directive): completely fill the CRM, cost no object.** Token/API
cost is not a constraint — paid tiers are assumed. Enrichment runs a **bulk
fill-everything** pass over the whole network *and* just-in-time top-ups for freshness
and the surfaced few. The one hard limit is honesty: with accuracy bias, a field that
can't be reliably determined stays **blank**, never guessed wrong (§11).

### Architecture in one paragraph
**Hybrid retrieval** (structured Postgres filter narrows → semantic match ranks)
over compact per-contact **capsules** that blend enriched public signal with your
private notes. Claude Code does the semantic matching and the live web research
in-context; the CLI emits capsules, filters, and dossiers and owns the write-path.
Enrichment runs in **two modes**: a **bulk fill-everything** pass over the whole
network (cost no object, paid tiers assumed) to populate every contact, plus
**just-in-time** top-ups for freshness and the long tail. Rate limiting / quota
counters remain — but for *throughput and safety*, not cost-avoidance (the goal is to
finish the fill, not to stay under a free tier). Every written value flows through one
atomic, advisory-locked Postgres RPC that guarantees **manual data is never clobbered**.

### Non-goals
- No sending/outreach/campaigns (unchanged charter — Claude Code drafts; you send).
- No LLM/embedding SDK inside the `crm` package (Option B: agents act, data stores).
- No LinkedIn scraping (hiQ → contract breach; Proxycurl sued into shutdown
  Jul 2025). PDL is the licensed last-resort substitute.
- **No password-reset / account-existence probing (holehe-style)** — it interacts
  with the person's account and signals them. Permanently out of scope.

## 2. Context: what already exists

The codebase is already a mini-MDM. Key substrate, mostly built for dedup:

- **`enrichment_log`** — append-only per-field record: `contact_id, field,
  old_value, new_value, source, confidence (real, nullable), method, created_at`
  (`supabase/migrations/0001_initial_schema.sql:128`). Field-level provenance.
  Schema comment (line ~127) explicitly anticipates **job-change history**.
- **`contacts`** golden table: `full_name, first_name, last_name, current_role,
  current_company, location, affiliations text[], origin_context, notes,
  tags text[], email_status enum(verified|risky|invalid|unknown, default unknown),
  closeness_tier, last_touchpoint_at/_channel/_topic, last_enriched_at, created_at,
  updated_at`.
- **`contact_identities`** — immutable, one row per `(source, source_external_id)`;
  holds `email, phone, linkedin_url, handle, raw_json`. **These are the keys dedup
  matches on** (`matching.py:35-73`, `dedup_plan.py:27-70`; exact keys =
  email/linkedin_url/phone). Unique index on `(source, source_external_id)`.
- **Survivorship today**: `_fill_and_log` (`dedup.py:54-73`) — existing non-null
  wins, incoming fills nulls, conflicts logged. Runs under **up to 16 parallel
  worker threads** (`dedup.py:181-220`) with **no transactions or row locks** in
  any Python path.
- **Atomic multi-row ops are Postgres RPCs**: `create_contacts_with_identities`
  (`0005:37`, "one transaction → no orphan window"), `backfill_recompute_contacts`
  (`0004:12`, moved server-side *specifically* to survive parallel workers).
- **Manual writes** (`crm set`, `contacts.py:207-209`): `method="manual_set"`,
  `source=<agent id>` (default `"rahul"`).
- Conventions: `--agent <id>` on mutating commands; `--json` on reads; `--dry-run`;
  exit codes 0 ok / 1 error / 2 usage; new subcommands register in `cli.py`; every
  mutating command calls `require_agent`. Tests: pytest against a **local** Supabase
  stack; `db` fixture truncates tables and refuses non-local URLs; no HTTP fixture
  exists yet. RPCs are tested via `db.rpc(...)` (`tests/test_recompute.py`).
- **The repo is PUBLIC** (scrubbed history, noreply identity). Anything committed
  is permanent.

## 3. Data model

### 3.1 Survivorship runs in ONE Postgres RPC — `enrich_apply_candidate` (load-bearing)

The single most important decision: **survivorship is a server-side RPC, not
client-side Python.** Client-side recompute is a read-modify-write across multiple
PostgREST calls and races the existing 16-worker dedup pool — corrupting the winner
and silently breaking "manual always wins." The repo already solved this twice
(`backfill_recompute_contacts`, `create_contacts_with_identities`).

```
enrich_apply_candidate(
  p_contact_id uuid, p_field text, p_value text,
  p_method text, p_source text, p_confidence real,
  p_source_detail text, p_dry_run boolean default false
) returns enum(golden | review | losing | noop)
```

In ONE transaction it:
1. Takes `pg_advisory_xact_lock(hashtext(p_contact_id::text || p_field))` —
   serializes all writes to that `(contact, field)` without locking the table.
2. Inserts the `enrichment_log` candidate row (append-only provenance) — unless an
   identical row already exists (idempotency, §3.6).
3. Recomputes the winner for `(contact_id, field)` by **method-class → recency →
   confidence** (see §3.2), excluding tombstoned values (§3.4).
4. If the winner clears the field's accept threshold and is not beaten by a `manual`
   row → sets it `is_current=true`, demotes the prior winner, materializes the value
   to `contacts.<field>` — all in this txn, so the partial-unique index never sees
   two currents.
5. If sub-threshold or in conflict → inserts an `enrich_review` row, leaves golden
   untouched.
6. `p_dry_run=true` → computes and returns the outcome without any mutation (powers
   `--dry-run`).

**Every write path funnels through this RPC**: `crm set`, `crm enrich run`,
`crm enrich apply`, `crm enrich review --approve`. `merge`/`split` must call a
recompute for affected fields (they rewrite `enrichment_log.contact_id` in bulk —
`dedup.py:371` — so they must re-elect a single winner per field afterward).

### 3.2 Extend `enrichment_log` into the provenance spine

New columns (additive, nullable/defaulted — safe migration):

| Column | Type | Purpose |
|---|---|---|
| `source_detail` | text | citation URL (web) / endpoint (API) — the provenance receipt |
| `verification_status` | enum(`unverified`,`verified`,`disputed`,`human_confirmed`), default `unverified` | drives review + email-tier + tombstones |
| `refresh_after` | date | per-field freshness clock; read by `crm enrich due` and the `stale` flag |
| `is_current` | bool, default false | the winning candidate for a scalar `(contact, field)` |

Partial unique index `(contact_id, field) WHERE is_current`.

**Canonical `method` scheme** (resolves the existing two-convention ambiguity).
Survivorship ranks on a **method-class**, never by string-parsing `source`:
- `manual_set` → class **manual** (top). Only human-edit paths emit it
  (`crm set`, `crm enrich review --approve`).
- `enrich_api` (source = plugin name, e.g. `gravatar`) → class **derived**.
- `enrich_agent` (source = `agent:<id>`, e.g. `agent:claude-web`) → class **derived**.
- `import_conflict` / dedup paths → class **derived** (unchanged).
- `legacy_import` → class **derived** (synthetic backfill, §3.3) — but a human-typed
  `add` is seeded as **manual** (see §3.3).

Within a class: recency → confidence. `confidence IS NULL` ranks as **0.4 for
ordering only** (never compared to the accept threshold — keeps legacy null-confidence
dedup rows from flooding review). The "pure-LLM-inference = below threshold" rung is a
distinct *explicit* 0.5, not the null sentinel.

**Confidence ladder** (bias to accuracy — a blank beats a wrong fact about a real
person): manual 1.0 (always wins) · verified API ~0.9 · broker ~0.8 · web-cited ~0.7
· pure LLM inference ~0.5 (→ review, never auto-golden). Confidence is **per-field,
not per-source** (§3.7).

### 3.3 One-time provenance backfill (migration step — prevents silent clobber)

Every existing non-null `contacts.<field>` has *no* provenance row, so the first
enrich run would treat it as a gap and overwrite it. The Phase-0 migration seeds a
synthetic `is_current=true` row per existing non-null scalar field:
- fields a human typed via `crm add`/`crm set` history → `method='manual_set'`,
  confidence 1.0 (sacred).
- fields originating from imports/dedup → `method='legacy_import'`, confidence ~0.8.
Where origin is indeterminable, default to `legacy_import` 0.8 (conservative:
competes rather than auto-wins, but a later verified source can supersede).

### 3.4 Reject = tombstone; deliberate blank = winning assertion

- `crm enrich review --reject` writes an `enrichment_log` row with
  `verification_status='disputed'` capturing the rejected `(field, value)`. The
  recompute RPC **excludes any candidate whose `(field, value)` has a `disputed`
  row** → rejections are sticky across re-runs.
- A deliberate blank (`crm set company=` empty) writes a `manual_set` row with
  `new_value=NULL`, `is_current=true`. The gap-detector treats "has an `is_current`
  manual row, even if NULL" as **not a gap** → enrichment won't refill it.
- Disputed-exclusion is **by `(field, value)` match, not row identity**: a later
  re-apply of a previously-rejected value still inserts its provenance row (audit
  trail intact) but is excluded from election and so can never become golden again.

### 3.5 Identifier vs attribute split (prevents enrichment from manufacturing duplicates)

Discovered **identifiers** (email, linkedin_url, phone) are dedup *keys*, not
attributes. Writing them only to `contacts` columns would make the next import miss
the match and create a duplicate. So `enrich apply`/`run` split by field kind:

- **Discovered identifier** → routed through `find_candidates` (`matching.py:35`)
  before any write:
  - 0 hits → insert a `contact_identities` row (`source="enrich:<plugin|agent>"`,
    `source_external_id=<value-hash>` → idempotent via the existing unique index).
  - 1 hit == this contact → no-op.
  - 1 hit == a **different** contact, or ≥2 hits → **do NOT insert**; create an
    `enrich_review` row (`reason='identifier_conflict'`) naming the other contact.
    Never auto-merge (reuses the `_attach_identity` conflict guard, `dedup.py:46`).
  - Unverified identifiers are **quarantined** (the `possible_*` pattern): stored
    but NOT a live match key until a verifying source confirms or a human approves.
    **Mechanism: a `candidate_identities` side table** (NOT a column on
    `contact_identities` — keeps that table truly immutable and leaves the dedup
    hot path / `find_candidates` matching semantics untouched). Rows promote into a
    real `contact_identities` row on confirmation/approval.
- **Discovered attribute** (role, company, company_category, expertise, avatar,
  location, birthday…) → `contacts.<field>` via the RPC (§3.1).

Enrichment-sourced emails get **stricter** role/shared-inbox screening than imports
(no human vouched for them).

### 3.6 `crm enrich apply` JSON schema (pinned) + idempotency

One canonical schema (resolves the §4-vs-§6 `citation`/`source_detail` mismatch).
Accepts a single object or an array:

```json
{ "field": "company_category",
  "value": "cybersecurity",
  "kind": "attribute",            // "attribute" | "identifier"
  "confidence": 0.8,              // validated 0–1 in the CLI (schema can't bound it)
  "source": "agent:claude-web",
  "source_detail": "https://techcrunch.com/...",   // citation URL / API endpoint
  "evidence": "Acme raised $15M Series A, May 2026" // short justification text
}
```

`evidence` is provenance context, not a golden value: it is stored on the
`enrichment_log` row by composing `source_detail` as `"<url> · <evidence>"` when both
are present (no separate column, no pollution of `notes`). `field` + `kind` decide the
write path (§3.5: `identifier` → `candidate_identities`/`contact_identities`;
`attribute` → the RPC).

**Idempotency:** a partial index / RPC guard treats an insert as a no-op when an
identical latest `(contact_id, field, new_value, source)` already exists — so re-running
an overnight batch doesn't double the log.

### 3.7 New columns on `contacts` + array handling

Capability signal (the matchable fields — Phase 0/2):
- `company_category` (text — "cybersecurity", "fintech"), `company_description` (text),
  `company_domain` (text), `expertise` (text[]), `interests` (text[]).

Relationship signal (designed now, **added in Plan 2's migration**, used in Phase 3):
- `birthday_year`/`birthday_month`/`birthday_day` (int, nullable — year often
  unknown; Monica's hard-won lesson), `cadence_days` (int).
- Spouse/kids/relationships are **NOT** flat columns — modeled later as a
  `relationships(contact_id, related_contact_id, type)` table (Monica pattern).

Avatar/social (cheap, low-priority): `avatar_url`, `github_username`, `twitter_username`,
`website_url`.

**Array fields (`tags[]`, `affiliations[]`, `expertise[]`, `interests[]`) are
EXCLUDED from the single-winner mechanism.** They use set-union semantics (existing
`merge` behavior, `dedup.py:379`); per-element provenance is logged with no
`is_current` participation; the materialized array is the union of non-rejected
elements. The recompute RPC does `WHERE field NOT IN (<array fields>)`. The existing
`crm set` array path is folded into the RPC to avoid lost-update under concurrency.

### 3.8 `enrich_review` (human-in-the-loop queue)

```sql
create table enrich_review (
  id uuid primary key default gen_random_uuid(),
  contact_id uuid not null references contacts(id) on delete cascade,
  field text not null,
  candidate_value text,
  source text not null,
  confidence real,
  reason text,                       -- 'low_confidence' | 'value_conflict' | 'identifier_conflict'
  other_contact_id uuid references contacts(id),  -- for cross-contact identifier conflicts
  status text not null default 'open',            -- open | resolved | skipped
  created_at timestamptz not null default now(),
  resolved_at timestamptz
);
create index on enrich_review (status, created_at);
```

Kept human-sized (§9): only queue low-confidence/conflict items for contacts above a
closeness/interaction bar; surface a daily slice ranked by `closeness_tier`; auto-expire
stale low-value items.

## 4. Retrieval layer (Phase 1 — the product)

### 4.1 Structured filters — `crm list`

Composable, all `--json`:
```
crm list --role founder,ceo,co-founder \  # NEW: case-insensitive match on current_role + synonym set
         --company-category cybersecurity \ # NEW
         --stage pre_seed,seed,series_a \   # NEW (derived on demand, §6)
         --location nyc \                    # NEW
         --cold-since 3 --status in_network --tag vip   # EXISTS
```
`--role` does substring + a small curated synonym expansion (founder/co-founder/ceo/
cofounder). A tunable `--role-class founder` alias maps to the maintained synonym set.

### 4.2 Capsules — `crm capsules --json`

One compact line per contact (~40 tokens) — the searchable representation, blending
**enriched public signal** + **your private signal**:
```json
{"name":"Dana Lee","role":"CTO","company":"SentinelMesh","company_category":"cybersecurity",
 "expertise":["zero-trust","appsec"],"tags":["security"],"note":"spoke at Black Hat",
 "topics":["eval tooling"],"location":"SF","tier":"t3_community","last":"2025-09","stale":true}
```
The chef is found from `note`/`tags`; the cybersecurity CTO from `company_category`/
`expertise`. Both halves are essential.

### 4.3 Hybrid match — `crm find "<intent>"` (and Claude-Code-driven)

Hybrid = **structured pre-filter (Postgres) → semantic rank**. Semantic stage
**starts in-context**: the CLI emits capsules for the pre-filtered set; Claude Code
reasons over them (no embeddings, no egress, fits your scale via prompt caching). The
`crm find` interface is **pgvector-ready** — when the network outgrows the context
window we add a `pgvector` embedding column + vector top-k behind the same command,
without changing the agent contract. (Embeddings, if added, use a local model or a
consent-gated embedding API — see §8 privacy.)

### 4.4 Dossier — `crm contact <ref> --json`

Returns the full bundle for a surfaced person: golden fields + per-field provenance +
`origin_context` (how/where you met) + interaction summaries (`topics` — what you
discussed) + `last_touchpoint` (when/what) + `stale` flags on volatile fields.
Pre-migration values render as plain values with no provenance suffix (graceful).

## 5. CLI commands (full surface)

Engine / cron: `crm enrich run <ref|--all|--due>` (waterfall; `--sources`, `--fields`,
`--dry-run`; per-record status `enriched/already_fresh/not_found/low_confidence→review/
quota_exhausted/error` — never a silent skip) · `crm enrich due` (freshness).
Agent seam: `crm enrich apply` (§3.6 door). Human: `crm enrich review`
(`--approve/--reject/--skip`, §3.4). **Recovery/ops:** `crm enrich undo <ref> <field>`
(demote the current robot value, re-elect the prior winner from the append-only log —
matches `merge`'s reversibility bar) · `crm enrich stats` (counts by status/source/
stale/in-review — fold into existing `crm stats`) · `crm enrich forget <ref>` (redaction:
null the value columns in provenance, keep structural rows — satisfies erasure against
an append-only log). Read: `crm list`, `crm capsules`, `crm find`, `crm contact` (§4).

## 6. Just-in-time enrichment + live research (Phase 2)

Two modes, same machinery:
- **Bulk fill-everything** (owner's primary goal) — `crm enrich run --all` over the
  whole network, paid tiers on, run to completion. Drive it as a resumable Batch-API +
  multi-source pass; the quota counter/token buckets exist for rate-limit safety and
  resumability, not to ration. This is what gets the CRM "completely filled."
- **Just-in-time** — enrich the ~40 a query surfaces, on demand, for freshness and the
  long tail. Funding **stage** is the canonical JIT field: not in any free clean source,
  so filter on `role` first, then derive stage via live web research on that set, cached
  with a short TTL (`stale` after ~45 days).

### 6.1 Deterministic source plugins (free tier)

Injectable typed registry (SpiderFoot-style; injectable so tests pass fakes):
```python
class Source(Protocol):
    name: str; cost_tier: int           # 0 local · 1 free API · 2 paid credit
    needs: set[str]; produces: dict[str, float]   # field -> per-field confidence
    def fetch(self, contact: dict) -> list[Candidate]
```
Order (cheapest-first, stop on a value passing its quality bar; verify *inside* the loop):

| Tier | Source | input → produces | Notes |
|---|---|---|---|
| 0 | local cache | — | fresh provenance row = instant hit |
| 1 | **Gravatar** | email → avatar, name, location, socials, website | **SHA256(email.strip().lower())**; `?d=404` HEAD probe for free avatar check; avoid legacy `gravatar.js` endpoint |
| 1 | **GitHub** | email/username → company, location, bio, twitter, site | email-search is **fuzzy → gate on `total_count==1`**; parse `noreply` emails first; `github.com/{user}.png` free avatar; **always use a PAT** |
| 1 | **Logo.dev** | company domain → logo, company name | 500k/mo free. **Drop Brandfetch** (100-lifetime trap) |
| 1 | email-verify (ZeroBounce/Abstract) | email → verified/risky/invalid/unknown | hygiene; sets `email_status`, gates the waterfall; 100/mo wall |
| 2 | **PDL** | name/email → role, company, socials | licensed LinkedIn substitute; **opt-in, last resort**; 100/mo wall |

Reference port: `taitems/user-email-enrichment` (Gravatar+GitHub+inference) + its
`pickBestName`/`pickTwitter` survivorship cascades + freemail-gated company-from-domain.

### 6.2 Rate / quota / cost control

- **Per-source token bucket** keyed on the API key (NOT dedup's per-worker pool —
  that's Postgres-only; the limit is per-key). Run 2–4 workers gated by buckets.
- **Persisted monthly quota counter** (`source_usage(source, month, count)`) that
  **hard-stops** metered sources (email-verify, PDL at 100/mo) and prevents the
  waterfall from cascading into PDL when email-verify is exhausted.
- Backoff: honor `Retry-After`, else exponential + jitter, cap ~60s, ~5 retries →
  mark field `error`, continue (per-source errors isolated, never abort the contact).
- **Stretch `refresh_after` aggressively** — steady-state monthly re-verify dominates
  cost; monthly email re-check is overkill for a personal CRM (6–12mo). Per-run
  anomaly guard: if a source's accept-rate/value-distribution spikes, pause → review.

### 6.3 The Claude consuming agent (live research)

A documented Claude Code skill (`docs/agents/enrich-agent.md`), **not** package code.
Loop per surfaced contact:
1. **Gate:** `crm enrich gaps <ref> --json` returns null/stale fields *after* the
   deterministic waterfall. Presence pre-check: 0 tier-1 hits + no company domain →
   `not_found`, spend no searches. `max_uses` scaled to gap count (1–4). A field
   NOT-FOUND twice gets a long `refresh_after` (no re-burning searches).
2. **Research call** — **Sonnet 4.6**, `web_search` ON (citations always on), **no
   structured output**. Domain-lock when known. Returns findings + real citations.
3. **Extract + judge call** — **Haiku 4.5**, structured output ON, no tools. Reads the
   research transcript; per candidate returns
   `{field, value, citation_url, cited_text, supported_by_citation: bool, n_sources: int}`.
   **No self-reported confidence.** (web_search + structured-output in ONE call is a
   400 — citations are non-optional and incompatible with `output_config.format`. Two
   calls is mandatory. Verify the exact 400 with a 10-line script before locking.)
4. **Confidence computed deterministically in `crm enrich apply`**, never by the LLM:
   `supported_by_citation=false` → hard cap 0.3 (→ review); `n_sources` 1/2/3+ →
   0.6/0.75/0.85; optional N=3 self-consistency (re-run step 2 only for fields about
   to cross auto-golden) → disagreement caps 0.5; field-volatility ceiling.
   Tier-B second Haiku judge for ≥0.7 candidates on identity-critical fields.
5. **Write back** via `crm enrich apply --agent claude-web`. Survivorship/review apply
   identically; the agent gets no power to overwrite manual data.

Model routing: Sonnet 4.6 research · Haiku 4.5 extract/judge · Opus 4.8 the slice-2
relationship brief (prose quality is the product). Cost: prompt-cache the system
prompt+schema; Batch API for any bulk pass (50% off tokens — but **not** web_search,
$10/1k); the **presence gate is the real cost lever** (skips ~40% with no footprint).
Worked: ~$0.04–0.07/contact bulk; JIT (40 surfaced) ≈ pennies/query.

### 6.4 The second-degree / connector boundary (be honest)

"Who in LA could connect me to a celebrity they know?" has two parts the system treats
differently:
- **Findable now:** *who is the likely bridge* — location=LA + an enriched
  `company_category`/role/expertise signal that says "entertainment-adjacent,
  well-connected" (talent/agency/media/exec) + closeness. Capsule + semantic match
  surfaces these people, and the dossier tells you how you know them. This is fully in
  scope.
- **NOT knowable from public data:** *who that bridge actually knows* — their private
  rolodex. We do **not** scrape or infer someone's real connections. The system
  surfaces the right person to *ask*; you ask them. (A coarse first-degree graph from
  your own LinkedIn export — mutual connections you both have — is the Phase-3 graph;
  it never extends to a contact's private network.)
A `connector`/`network_reach` signal (is this person notably well-networked in domain
X) is an **inferred capability** the agent can populate into `expertise`/`tags` from
public signal (their role, follower counts, press) — labeled as inference, never as
fact about specific relationships.

## 7. Job-change detection (free, Phase 0 payoff)

Route LinkedIn re-imports' role/company writes through the provenance RPC. When
`current_company` changes from a non-null old value to a different new value, that's a
**detected job change**, logged for free by survivorship. `crm changes --since <date>`
surfaces them → the canonical "congrats on the new role" reconnect trigger. Reframes
the provenance spine's first payoff as change-detection, not avatar citations.

## 8. Privacy & security

- **Per-contact consent model** gating outward sources (web_search, PDL, SMTP-verify)
  separately from self-published ones (Gravatar/GitHub/Logo.dev). Outward reach is
  deliberate per person (a `consent`/`--allow-web` flag), **never** blanket `--all`.
  web_search queries are domain-locked where possible and must not concatenate name +
  sensitive inferred attributes (search queries leave Anthropic's data terms — they go
  to a third-party search provider).
- **Egress is logged**: every external call's source + citation lands in provenance →
  answers a future "where did you get this?" (GDPR Art. 14 / CA Delete Act). PDL is
  tagged with broker name + date.
- **Erasure vs append-only log**: `crm enrich forget` redacts value columns, keeps
  structural rows; `enrichment_log` cascades/redacts on contact delete (decide: redact).
- **Public-repo guardrails**: plugin test fixtures are **synthetic only** (Ada Lovelace
  style) — never recorded real responses. Pre-add `.gitignore` for any cache
  (`.crm-cache/`, `*.enrich.json`). The `pre-publish` skill is a gate before any push.
- Gravatar hashes are reversible (correlation signal) — covered by the consent model.

## 9. Build phases (value-first, risk-deferred)

**This spec ships as TWO implementation plans** (it spans four distinct risk/test
surfaces — a Postgres survivorship engine, a retrieval product, a multi-source
pipeline, and an LLM agent contract — too big for one plan):
- **Plan 1 = Phase 0 + Phase 1** — the RPC substrate + retrieval product. Zero
  external network/LLM; answers the driving use cases off existing data. One coherent,
  testable unit with a clean seam (the RPC + the `enrich apply` JSON contract). Its
  migration adds only the columns Phase 0/1 use (`company_category`,
  `company_description`, `company_domain`, `expertise`, `interests`, the provenance
  columns, `enrich_review`, `candidate_identities`).
- **Plan 2 = Phase 2 (+ Phase 3)** — JIT enrichment, source plugins, the Claude agent,
  quota/consent machinery; and later the relationship graph + the Phase-3 relationship
  columns (`birthday_*`, `cadence_days`, the `relationships` table) in its own
  migration. Consumes Plan 1's `enrich apply` door without changing it. The §11
  `web_search`+structured-output 400 verification is a Plan-2 gate, not a Plan-1 one.

- **Phase 0 — Substrate + first wins.** `enrich_apply_candidate` RPC (advisory-locked,
  dry-run); enrichment_log extensions; **synthetic-provenance backfill migration**;
  tombstone-reject + sticky-NULL; identifier/attribute split + quarantine; `enrich_review`;
  capability columns; `crm enrich apply` (pinned schema, idempotent) + `review` + `undo`
  + `stats` + `forget`; injectable Source seam; **job-change detection**; provenance
  rendering in `crm contact`. Visible day-one: provenance lights up across the CRM,
  job-changes surface. (Optionally pull Gravatar in here for an immediate real source.)
- **Phase 1 — Retrieval (the daily product).** `crm list` structured filters,
  `crm capsules`, hybrid `crm find`, the rich `crm contact` dossier. Answers
  "who could teach me cybersecurity / who's a founder I've gone cold on" off data you
  already have. Zero external enrichment required.
- **Phase 2 — JIT enrichment + live research.** Deterministic free sources
  (Gravatar/GitHub/Logo.dev) for capsule enrichment; the two-call Claude agent for
  live company-category/expertise/stage/news, on-demand and segment-scoped; freshness
  clock; quota counter + token buckets + consent gating; PDL opt-in.
- **Phase 3 — Extensions.** Relationship graph (`crm who-knows <company>`, warm-intro
  paths), reconnect digest, `pgvector` semantic if scale demands, relationship columns
  (birthday/cadence) + the `relationships` table + the Opus "before you reach out" brief.

## 10. Testing

Pytest against local Supabase (`db` fixture truncates, refuses non-local). Two decoupled
seams:
- **Survivorship RPC** tested directly via `db.rpc(...)` (like `test_recompute.py`) — the
  load-bearing write-path tests need **zero network/LLM**: manual never clobbered;
  conflict → review; tombstone sticks across re-runs; deliberate NULL not refilled;
  array fields union (not overwrite); identifier-discovery writes `contact_identities`
  and a later import auto-matches (no duplicate); identifier collision → review;
  idempotent re-apply (no duplicate provenance); dry-run mutates nothing;
  **concurrency**: two threads applying to the same `(contact, field)` → exactly one
  `is_current`, golden matches it.
- **Source plugins** via an injectable `FakeSource` (engine tests: waterfall stops on
  first quality hit; `invalid` email is not a hit; quota-exhausted ≠ not_found) +
  per-plugin parsing tests using `respx` against **synthetic** checked-in fixtures.
- The Claude agent is not package code → no LLM mocking; its contract is the
  `enrich apply` JSON schema (a schema-validation test).

## 11. Risks & open questions

- **"Completely filled" has a ceiling.** Cost-no-object removes the *budget* limit, not
  the *availability* limit: many contacts have little public footprint, and accuracy
  bias means we leave a field blank rather than write a wrong value. Expect high fill
  on professional fields for findable people (founders/execs/devs), sparse fill on
  low-footprint personal contacts. Success = "every reliably-knowable field is filled
  and sourced," not "no blanks anywhere." Coverage is reported by `crm enrich stats`.
- Verify the exact `web_search` + structured-output 400 with a live 10-line script
  before locking §6.3 as load-bearing (the citations incompatibility forces two calls
  regardless).
- Funding-stage accuracy/freshness — accept that "stage" is best-effort, JIT, TTL'd.
- `pgvector` upgrade trigger — define the contact-count/cost threshold at which
  in-context retrieval stops paying.
- email-verify provider choice (free tier at build time) — behind the plugin contract,
  swappable.
- Model lineup/pricing drift — verify current Opus/Sonnet/Haiku ids + web_search rate
  at implementation (use the `claude-api` skill).

## 12. Provenance of this design

Grounded in six parallel research streams (Claude/LLM enrichment tactics; commercial
waterfall landscape; personal-CRM relationship signals; privacy-safe data sources;
enrichment engineering patterns; OSS prior art) and an **8-way adversarial review**
that reshaped it: adversarial-architecture (RPC survivorship, array fields, tombstones,
is_current races), privacy/legal red-team (consent model, erasure, public-repo
fixtures, web_search egress), entity-resolution (identifier→`contact_identities`
routing, quarantine, per-field confidence), Claude-agent expert (two-call design,
deterministic confidence, grounding judge, model routing), cost/scale (quota walls at
N≈100, token buckets, quota counter, Logo.dev-not-Brandfetch), product insight
(retrieval/job-change > avatar backfill), OSS mining (Gravatar SHA256/`?d=404`, GitHub
`total_count==1`/noreply, Monica date+relationship schema), and implementation
premortem (RPC-vs-Python, testing seams, migration backfill, `undo`/`stats`/`forget`,
visible-value slicing).
