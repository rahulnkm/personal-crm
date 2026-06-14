"""Pure enrichment helpers — payload parsing + candidate model. No DB access.

The enrich write-path funnels through two kinds of facts:
  - ATTRIBUTE: a scalar golden-record field (current_company, location, ...) →
    survivorship RPC arbitrates it.
  - IDENTIFIER: a hard match key (email, linkedin_url, phone, handle) → quarantined
    into candidate_identities until a human promotes it (never silently a match key).

Keeping this module DB-free makes it unit-testable without a stack.
"""
import json
from dataclasses import dataclass, field as dc_field

ATTRIBUTE = "attribute"
IDENTIFIER = "identifier"

# fields that are hard match keys, not golden attributes — routed to quarantine
IDENTIFIER_FIELDS = {"email", "linkedin_url", "phone", "handle"}


@dataclass
class EnrichCandidate:
    field: str
    value: str | None
    kind: str
    confidence: float | None = None
    source: str = ""
    source_detail: str | None = None
    evidence: str | None = None


def _one(obj: dict) -> EnrichCandidate:
    if not isinstance(obj, dict):
        raise ValueError(f"Each candidate must be a JSON object, got {type(obj).__name__}")
    field = obj.get("field")
    if not field:
        raise ValueError("Candidate missing required 'field'")
    conf = obj.get("confidence")
    if conf is not None:
        conf = float(conf)
        if not (0.0 <= conf <= 1.0):
            raise ValueError(f"confidence must be in [0,1], got {conf}")
    kind = obj.get("kind")
    if kind is None:
        kind = IDENTIFIER if field in IDENTIFIER_FIELDS else ATTRIBUTE
    if kind not in (ATTRIBUTE, IDENTIFIER):
        raise ValueError(f"kind must be '{ATTRIBUTE}' or '{IDENTIFIER}', got {kind!r}")

    source_detail = obj.get("source_detail")
    evidence = obj.get("evidence")
    # fold evidence into source_detail so provenance carries both the URL and the
    # human-readable justification in one column
    if source_detail and evidence:
        source_detail = f"{source_detail} · {evidence}"
    elif evidence and not source_detail:
        source_detail = evidence

    return EnrichCandidate(
        field=field,
        value=obj.get("value"),
        kind=kind,
        confidence=conf,
        source=obj.get("source", ""),
        source_detail=source_detail,
        evidence=evidence,
    )


def parse_payload(json_str: str) -> list[EnrichCandidate]:
    """Parse a JSON object or array of candidate facts into EnrichCandidate list."""
    data = json.loads(json_str)
    items = data if isinstance(data, list) else [data]
    return [_one(o) for o in items]
