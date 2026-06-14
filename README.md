# personal-crm

Pure data layer for your real-world network. One golden record per human, fed by
importers through staging + entity-resolution dedup, served to agents via the
`crm` CLI. No sending, no campaigns — consuming agents do that.

## Setup
Prereqs: [Docker](https://docs.docker.com/get-docker/) (for the local Supabase stack) and [uv](https://docs.astral.sh/uv/).

1. `brew install supabase/tap/supabase && supabase start` (local dev; needs Docker)
2. `mkdir -p ~/.crm && cp .env.example ~/.crm/.env` — fill in URL + secret key from `supabase status`
3. `uv tool install --editable .`
4. `supabase db reset` (applies migrations)

> **Single-tenant by design.** The app authenticates with the Supabase secret/service-role
> key, which bypasses Row Level Security. RLS is enabled with no policies (deny-all for other
> roles). Do **not** expose an `anon`/`authenticated` key against this schema without writing
> RLS policies first.

## The loop

```
# Import people (native sources — run in order; names must exist before handles)
crm import apple-contacts            # one staging row per email/phone
crm dedup                            # resolve staging → golden records
crm import linkedin <zip|csv>        # people + connected-on touchpoints
crm dedup
crm import imessage                  # per-handle touchpoints (needs apple-contacts first)

# Import people from any CSV
crm import csv <file> --source <slug> --map "first_name=First Name,last_name=Last Name,email=Email"
crm dedup

# Stage touchpoints from any CSV (agent-extracted: Gmail, Telegram, Luma, Substack)
crm import touchpoints <file> --source <slug> \
    --map "email=Email,occurred_at=Date,summary=What[,event_name=Event]" \
    --kind event --channel irl

# Link staged touchpoints → interactions, upgrade closeness tiers
crm backfill
crm backfill --retry-orphans         # after importing more people, recover orphans

# Promote people with a real touchpoint tier (t1/t2) to in_network (idempotent)
crm sync-status                       # re-run after backfill; --dry-run to preview

# Review & query
crm review       # arbitrate ambiguous matches (--approve/--reject/--to)
crm list --status in_network --cold-since 6   # who to reconnect with
crm contact "<name>"                          # full context for drafting
crm log / crm event add                       # record touchpoints manually

# Bulk writes — always dry-run first, then --yes to apply
crm bulk set status=in_network --affiliation YC --dry-run
crm bulk set status=in_network --affiliation YC --yes

crm bulk tag investors --affiliation YC --dry-run
crm bulk tag investors --affiliation YC --yes

crm bulk log --kind event --channel irl --summary "NeurIPS 2025" \
    --tag ml-researchers --dry-run
crm bulk log --kind event --channel irl --summary "NeurIPS 2025" \
    --tag ml-researchers --yes

# Cohort filters (same as crm list): --status --tier --tag --affiliation --cold-since
# Safety: a write always needs --yes; pass --all only to act on the entire table.
# Machine output: --json emits {dry_run, cohort_count, affected, changed_count}
```

Agent-run extractions (Gmail, Telegram, Luma, Substack) need no bespoke code —
see `docs/operational-loads.md` for the playbook.

Every mutating command takes `--agent <id>` (register with `crm agent register <id> --desc "..."`).
Machine output: add `--json`. Exit codes: 0 ok, 1 error, 2 usage.

## Notes
- Tests run ONLY against the local stack and truncate data tables — don't point
  tests at a database you care about (the fixture refuses non-local URLs).
- Cloud deploy: create a Supabase project, `supabase link` + `supabase db push`,
  point ~/.crm/.env at it (keep local creds in ./.env.local for tests), and cron
  `supabase db dump` weekly — the free tier has no backups.
- `"current_role"` is a quoted identifier in raw SQL (Postgres reserved word);
  transparent via the CLI/API.

## License
MIT — see [LICENSE](LICENSE).
