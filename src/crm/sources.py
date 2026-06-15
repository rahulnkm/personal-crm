"""HTTP source layer — deterministic, free public-signal enrichment sources.

Each source takes a contact email and returns a list of `Candidate` facts that the
`crm enrich run` driver feeds into the `enrich_apply_candidate` RPC. Sources never
raise on network failure: a 429 / timeout / 404 yields `[]` (logged), so one flaky
source never aborts a contact (spec §6.2: per-source errors isolated).

Mapping rules (spec §6.1, pre-researched):
  - Gravatar (no auth): SHA256(email.strip().lower()); HEAD avatar ?d=404 probe;
    profile JSON → location/company/role/twitter/website. full_name is the golden
    key and is NEVER mapped.
  - GitHub (token-optional): parse `noreply` emails locally; else, only with a token,
    fuzzy email-search gated on total_count==1; user JSON → company/location/blog/
    twitter/avatar. Free avatar at github.com/{login}.png.
"""
from __future__ import annotations

import hashlib
import logging
import os
import random
import re
import time
from typing import NamedTuple

import httpx

log = logging.getLogger("crm.sources")

# per-request network budget; failures degrade to [] rather than blocking the run.
TIMEOUT = httpx.Timeout(8.0)

GRAVATAR_CONFIDENCE = 0.9
GITHUB_CONFIDENCE = 0.85


class Candidate(NamedTuple):
    """A single discovered fact: (field, value, confidence, source_detail).

    source_detail is the provenance receipt (the endpoint/URL the value came from).
    """
    field: str
    value: str | None
    confidence: float
    source_detail: str | None = None


# HTTP 429 backoff (spec §6.2): honor Retry-After, else exponential + jitter, capped.
_MAX_RETRIES = 3
_BACKOFF_CAP = 60.0


def _request(method: str, url: str, **kw) -> httpx.Response | None:
    """One HTTP call with bounded 429 backoff. Returns the response (any non-429
    status) or None on exhausted retries / network error. Never raises."""
    attempt = 0
    while True:
        try:
            r = httpx.request(method, url, timeout=TIMEOUT, follow_redirects=True, **kw)
        except httpx.HTTPError as exc:
            log.warning("%s %s failed: %s", method, url, exc)
            return None
        if r.status_code != 429 or attempt >= _MAX_RETRIES:
            return r
        retry_after = r.headers.get("Retry-After")
        if retry_after and retry_after.isdigit():
            delay = min(float(retry_after), _BACKOFF_CAP)
        else:
            delay = min(2.0 ** attempt + random.random(), _BACKOFF_CAP)
        log.warning("%s %s → 429, backoff %.1fs (attempt %d)", method, url, delay,
                    attempt + 1)
        time.sleep(delay)
        attempt += 1


def gravatar_hash(email: str) -> str:
    """Gravatar identity hash: SHA256 of the trimmed, lowercased email (spec §6.1)."""
    return hashlib.sha256(email.strip().lower().encode()).hexdigest()


class GravatarSource:
    """Gravatar avatar + profile. No auth. Self-published signal."""

    name = "gravatar"
    produces = {"avatar_url", "location", "current_company", "current_role",
                "twitter_username", "website_url"}

    def fetch(self, email: str) -> list[Candidate]:
        if not email or "@" not in email:
            return []
        h = gravatar_hash(email)
        out: list[Candidate] = []

        avatar_url = f"https://gravatar.com/avatar/{h}"
        # ?d=404 → HEAD returns 200 only when a real avatar exists (else 404).
        r = _request("HEAD", avatar_url, params={"d": "404"})
        if r is not None and r.status_code == 200:
            out.append(Candidate("avatar_url", avatar_url, GRAVATAR_CONFIDENCE,
                                 avatar_url))

        profile_url = f"https://api.gravatar.com/v3/profiles/{h}"
        r = _request("GET", profile_url)
        if r is not None and r.status_code == 200:
            try:
                out += self._map_profile(r.json(), profile_url)
            except ValueError as exc:  # malformed JSON
                log.warning("gravatar profile parse failed for %s: %s", h, exc)
        elif r is not None and r.status_code != 404:
            log.warning("gravatar profile %s → HTTP %s", h, r.status_code)

        return out

    def _map_profile(self, data: dict, detail: str) -> list[Candidate]:
        out: list[Candidate] = []

        def add(field: str, value):
            if value and str(value).strip():
                out.append(Candidate(field, str(value).strip(), GRAVATAR_CONFIDENCE,
                                     detail))

        # full_name (display_name) is the golden key — deliberately NOT mapped.
        add("location", data.get("location"))
        add("current_company", data.get("company"))
        add("current_role", data.get("job_title"))

        # twitter from verified_accounts (shortname == 'twitter')
        for acct in data.get("verified_accounts") or []:
            short = (acct.get("shortname") or acct.get("service_label") or "").lower()
            if short == "twitter" and acct.get("username"):
                add("twitter_username", acct["username"])
                break

        # website = first non-social link
        for link in data.get("links") or []:
            url = (link.get("url") or "").strip()
            if url and not _is_social(url):
                add("website_url", url)
                break

        return out


_SOCIAL_HOSTS = ("twitter.com", "x.com", "instagram.com", "facebook.com",
                 "linkedin.com", "github.com", "youtube.com", "tiktok.com",
                 "mastodon", "threads.net")


def _is_social(url: str) -> bool:
    u = url.lower()
    return any(host in u for host in _SOCIAL_HOSTS)


# id+user@users.noreply.github.com  OR  user@users.noreply.github.com
_NOREPLY_RE = re.compile(
    r"^(?:\d+\+)?(?P<user>[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)"
    r"@users\.noreply\.github\.com$", re.IGNORECASE)


class GitHubSource:
    """GitHub user profile. Token-optional. Self-published signal."""

    name = "github"
    produces = {"github_username", "avatar_url", "current_company", "location",
                "website_url", "twitter_username"}

    def fetch(self, email: str) -> list[Candidate]:
        if not email or "@" not in email:
            return []
        token = os.environ.get("GITHUB_TOKEN")

        login = self._login_from_noreply(email)
        if login is None:
            # only spend an API call (fuzzy email search) if a token is present
            if not token:
                return []  # not an error — just no signal without a token/noreply
            login = self._login_from_search(email, token)
            if login is None:
                return []

        return self._fetch_user(login, token)

    @staticmethod
    def _login_from_noreply(email: str) -> str | None:
        m = _NOREPLY_RE.match(email.strip())
        return m.group("user") if m else None

    def _login_from_search(self, email: str, token: str) -> str | None:
        r = _request("GET", "https://api.github.com/search/users",
                     params={"q": email}, headers=self._headers(token))
        if r is None or r.status_code != 200:
            if r is not None:
                log.warning("github search → HTTP %s", r.status_code)
            return None
        try:
            data = r.json()
        except ValueError as exc:
            log.warning("github search parse failed for %s: %s", email, exc)
            return None
        # email search is fuzzy — trust the login ONLY on an exact single match.
        if data.get("total_count") == 1 and data.get("items"):
            return data["items"][0].get("login")
        return None

    def _fetch_user(self, login: str, token: str | None) -> list[Candidate]:
        url = f"https://api.github.com/users/{login}"
        r = _request("GET", url, headers=self._headers(token))
        if r is None or r.status_code != 200:
            if r is not None:
                log.warning("github user %s → HTTP %s", login, r.status_code)
            return []
        try:
            u = r.json()
        except ValueError as exc:
            log.warning("github user parse failed for %s: %s", login, exc)
            return []

        out: list[Candidate] = [
            Candidate("github_username", login, GITHUB_CONFIDENCE, url),
            # free avatar — stable, no token needed
            Candidate("avatar_url", f"https://github.com/{login}.png",
                      GITHUB_CONFIDENCE, url),
        ]

        def add(field: str, value):
            if value and str(value).strip():
                out.append(Candidate(field, str(value).strip(), GITHUB_CONFIDENCE, url))

        add("current_company", u.get("company"))
        add("location", u.get("location"))
        add("website_url", u.get("blog"))
        add("twitter_username", u.get("twitter_username"))
        return out

    @staticmethod
    def _headers(token: str | None) -> dict:
        h = {"Accept": "application/vnd.github+json",
             "X-GitHub-Api-Version": "2022-11-28"}
        if token:
            h["Authorization"] = f"Bearer {token}"
        return h


# cheapest-first registry (spec §6.1 source order). Injectable: tests pass fakes by
# selecting by name or constructing their own list.
SOURCES = [GravatarSource(), GitHubSource()]
_BY_NAME = {s.name: s for s in SOURCES}


def get_source(name: str):
    """Return the source instance with this name, or None."""
    return _BY_NAME.get(name)


def select_sources(names: list[str] | None):
    """Resolve a list of source names to instances (preserving registry order).

    None / empty → all sources. Unknown names are dropped (caller validates/reports).
    """
    if not names:
        return list(SOURCES)
    wanted = {n.strip().lower() for n in names if n.strip()}
    return [s for s in SOURCES if s.name in wanted]
