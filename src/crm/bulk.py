"""Shared helpers + constants for cohort-wide bulk operations (crm bulk *).

`CHUNK` bounds write batches (per-statement payloads / `.in_()` URL length) and
`PAGE` bounds cohort-read pagination. Both are module constants so tests can
monkeypatch them to small values for fast boundary coverage. `PAGE` here is
independent of `backfill.PAGE`. `_resolve_cohort` and the bulk command gate land
in a later task.
"""

CHUNK = 500
PAGE = 1000
