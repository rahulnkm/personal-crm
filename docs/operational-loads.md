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

## Order of native imports (matters)
apple-contacts → dedup → linkedin → dedup → imessage → backfill
(names first, then handles can match)
