# Operational loads — agent-run extractions

These sources need no bespoke importer: an agent with the right MCP/file access
produces two CSVs and uses the generic commands.

## Pattern
1. People CSV  → `crm import csv <f> --source <slug> --map "full_name=…,email=…"`
2. Touchpoints CSV (must include a match-key column) →
   `crm import touchpoints <f> --source <slug> --map "email=…,occurred_at=…,summary=…[,event_name=…]" --kind <k> --channel <ch>`
3. `crm dedup && crm backfill` (then `crm backfill --retry-orphans` after later imports)

## Sources
- **Telegram** (agent w/ Telegram MCP): list dialogs → people CSV (name, username
  as handle) + touchpoints CSV (handle, last-message date, kind=message,
  channel=telegram). Handle match key = telegram username; people CSV maps
  `handle=username` so identities carry it.
- **Gmail** (agent w/ Gmail MCP): sweep SENT mail (sent-to beats received-from
  for "people I actually talk to") → people CSV (name, email) + touchpoints CSV
  (email, last-thread date, kind=email, channel=email).
- **Luma** (Rahul downloads guest CSV per hosted event): touchpoints CSV with
  `event_name`/`event_location` per row, kind=event, channel=irl → creates the
  shared event + per-guest interactions. Also import the people CSV first.
- **Substack** (Rahul downloads subscriber CSV): people CSV (email) + touchpoints
  (email, subscribed date, kind=origin, channel=email).

## Cohort actions
After import / dedup / backfill, agents can act on the resolved contacts:

```
# set a scalar field on a filtered cohort
crm bulk set status=in_network --affiliation YC --dry-run --json
crm bulk set status=in_network --affiliation YC --yes --json

# add a registry tag (idempotent; reports cohort size vs newly-tagged count)
crm bulk tag investors --affiliation YC --dry-run --json
crm bulk tag investors --affiliation YC --yes --json

# log the same touchpoint against a cohort
crm bulk log --kind event --channel irl --summary "NeurIPS 2025" \
    --tag ml-researchers --dry-run --json
crm bulk log --kind event --channel irl --summary "NeurIPS 2025" \
    --tag ml-researchers --yes --json
```

`--json` emits `{dry_run, cohort_count, affected, changed_count}` — use for
agent-parseable output. Cohort filters are the same five as `crm list`
(`--status`, `--tier`, `--tag`, `--affiliation`, `--cold-since`). A bare call
with no filter is refused; pass `--all` to act on the entire table.
`bulk set` handles scalar fields only — for tags use `bulk tag`.

## Order of native imports (matters)
apple-contacts → dedup → linkedin → dedup → imessage → backfill
(names first, then handles can match)
