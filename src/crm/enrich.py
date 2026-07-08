"""Pure enrichment helpers — payload parsing + candidate model. No DB access.

The enrich write-path funnels through two kinds of facts:
  - ATTRIBUTE: a scalar golden-record field (current_company, location, ...) →
    survivorship RPC arbitrates it.
  - IDENTIFIER: a hard match key (email, linkedin_url, phone, handle) → quarantined
    into candidate_identities until a human promotes it (never silently a match key).

Keeping this module DB-free makes it unit-testable without a stack.
"""
import json
import re
from dataclasses import dataclass, field as dc_field

ATTRIBUTE = "attribute"
IDENTIFIER = "identifier"

# fields that are hard match keys, not golden attributes — routed to quarantine
IDENTIFIER_FIELDS = {"email", "linkedin_url", "phone", "handle"}

# array (set-union) attribute fields. These are still kind=ATTRIBUTE (not identifiers),
# but they have no single survivorship winner — they accumulate via enrich_apply_array
# instead of enrich_apply_candidate.
ARRAY_FIELDS = {"tags", "affiliations", "expertise", "interests"}

# agents naturally say "company"/"role"/"title"; the golden columns are prefixed.
# Normalize to the real column names so the survivorship RPC can materialize them
# (an unmapped "company" would hit a non-existent column and crash the RPC).
FIELD_ALIASES = {
    "company": "current_company",
    "role": "current_role",
    "title": "current_role",
    "job_title": "current_role",
}


@dataclass
class EnrichCandidate:
    field: str
    value: str | None
    kind: str
    confidence: float | None = None
    source: str = ""
    source_detail: str | None = None
    evidence: str | None = None


# ----- apply-time validation gate (agent writes only; manual set/note/add never
# ----- pass through apply, so their exemption is structural, not flagged) -----

# narrative fields whose claims must carry a grounding span in source_detail
NARRATIVE_FIELDS = {"origin_context", "notes"}
# minimum source_detail length for narrative/expertise writes. Length+presence
# only — a >=20-char pointer still passes; span-ness isn't machine-checkable here.
MIN_SOURCE_DETAIL_LEN = 20
# expertise elements are typed facets: tool|skill|role|domain, then a kebab slug
EXPERTISE_FACET_RE = re.compile(r"^(tool|skill|role|domain):[a-z0-9-]+$")

_SPAN_ERR = (
    f"needs source_detail of >={MIN_SOURCE_DETAIL_LEN} chars quoting the span the claim "
    "came from — the actual words (message/email/page text). A pointer to where you "
    "looked (a phone number + date range is not a span) doesn't let anyone verify the claim."
)


def gate_candidate(cand: EnrichCandidate) -> tuple[str, str] | None:
    """Pre-write check for narrative/expertise candidates.

    Returns (outcome, error) — 'rejected_bad_facet' or 'rejected_ungrounded' —
    or None if the candidate may proceed to the RPCs. Other fields pass untouched.
    """
    if cand.field == "expertise":
        v = (cand.value or "").strip()
        if v.startswith("["):
            return ("rejected_bad_facet",
                    "expertise element looks like a stringified JSON array "
                    f"({v[:40]!r}) — send one facet per candidate, not the array "
                    "serialized into a single element")
        if not EXPERTISE_FACET_RE.match(v):
            return ("rejected_bad_facet",
                    f"expertise element {v!r} must match "
                    "^(tool|skill|role|domain):[a-z0-9-]+$ (lowercase kebab slug)")
    if cand.field in NARRATIVE_FIELDS or cand.field == "expertise":
        detail = (cand.source_detail or "").strip()
        if len(detail) < MIN_SOURCE_DETAIL_LEN:
            return ("rejected_ungrounded", f"{cand.field} {_SPAN_ERR}")
    return None


def _one(obj: dict) -> EnrichCandidate:
    if not isinstance(obj, dict):
        raise ValueError(f"Each candidate must be a JSON object, got {type(obj).__name__}")
    field = obj.get("field")
    if not field:
        raise ValueError("Candidate missing required 'field'")
    field = FIELD_ALIASES.get(field, field)
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
