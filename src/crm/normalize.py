"""Canonical forms computed BEFORE any matching; raw originals stay in raw_json.

Most under-merging in practice is a normalization bug, not a matching bug —
'(415) 555-1234' must equal '+14155551234' before match keys are compared.
"""
import re
import unicodedata
from urllib.parse import unquote, urlparse

import phonenumbers

DEFAULT_REGION = "US"


def normalize_email(value: str | None) -> str | None:
    if not value:
        return None
    v = value.strip().lower()
    if "@" not in v or " " in v:
        return None
    local, _, domain = v.partition("@")
    # minimal structural check — not RFC validation (that's Plan 3's verify pass);
    # just enough to stop free-text containing '@' from becoming a match key
    if not local or "." not in domain or not domain.split(".")[-1]:
        return None
    return v


def normalize_phone(value: str | None) -> str | None:
    """E.164 or None. NOTE: extensions (x123) are dropped — two people sharing
    a main line + different extensions normalize to the same key."""
    if not value:
        return None
    try:
        parsed = phonenumbers.parse(value, DEFAULT_REGION)
    except phonenumbers.NumberParseException:
        return None
    if not phonenumbers.is_valid_number(parsed):
        return None
    return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)


def normalize_linkedin(value: str | None) -> str | None:
    if not value:
        return None
    v = value.strip().lower()
    if not v.startswith(("http://", "https://")):
        v = "https://" + v
    parsed = urlparse(v)
    host = (parsed.hostname or "").removeprefix("www.").removeprefix("m.")
    if host != "linkedin.com":
        return None
    path = unquote(parsed.path).rstrip("/")
    # require /in/<slug> — a company page or bare /in is NOT a person and
    # must never become an exact match key
    parts = path.split("/")
    if len(parts) < 3 or parts[1] != "in" or not parts[2]:
        return None
    return f"linkedin.com{path}"


def _social_handle(value: str, hosts: set[str], pattern: str) -> str | None:
    """URL / @handle / bare handle → bare lowercased handle, or None if it
    doesn't resolve to a valid handle on the expected host (blank beats wrong)."""
    v = value.strip().lower().removeprefix("@")
    if "/" in v or "." in v:  # URL form, not a bare handle
        u = v if v.startswith(("http://", "https://")) else "https://" + v
        parsed = urlparse(u)
        host = (parsed.hostname or "").removeprefix("www.").removeprefix("mobile.")
        if host not in hosts:
            return None
        parts = unquote(parsed.path).strip("/").split("/")
        v = parts[0] if parts else ""
    return v if re.fullmatch(pattern, v) else None


def normalize_twitter(value: str | None) -> str | None:
    if not value:
        return None
    return _social_handle(value, {"twitter.com", "x.com"}, r"[a-z0-9_]{1,15}")


def normalize_github(value: str | None) -> str | None:
    if not value:
        return None
    return _social_handle(value, {"github.com"}, r"[a-z0-9](?:[a-z0-9-]{0,38})?")


def normalize_name(value: str | None) -> str | None:
    if not value:
        return None
    v = unicodedata.normalize("NFKD", value)
    v = "".join(c for c in v if not unicodedata.combining(c))
    v = re.sub(r"\s+", " ", v).strip().casefold()
    return v or None
