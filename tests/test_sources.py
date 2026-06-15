"""Source-plugin parsing tests — respx against SYNTHETIC fixtures only.

NEVER hit real Gravatar/GitHub. Fixtures are Ada-Lovelace-style fakes (spec §8:
public-repo guardrail — no recorded real responses).
"""
import httpx
import pytest
import respx

from crm.sources import (
    Candidate,
    GitHubSource,
    GravatarSource,
    get_source,
    gravatar_hash,
)


# ----- gravatar_hash -----

def test_gravatar_hash_is_sha256_of_normalized_email():
    import hashlib
    # spec: sha256(email.strip().lower())
    expected = hashlib.sha256("ada@example.com".encode()).hexdigest()
    assert gravatar_hash("  Ada@Example.COM  ") == expected
    assert len(gravatar_hash("ada@example.com")) == 64


# ----- GravatarSource -----

GRAVATAR_PROFILE = {
    "display_name": "Ada Lovelace",
    "location": "London, UK",
    "job_title": "Analytical Engine Programmer",
    "company": "Analytic Engines Ltd",
    "verified_accounts": [
        {"service_label": "Twitter", "shortname": "twitter", "username": "adalovelace"},
        {"service_label": "Instagram", "shortname": "instagram", "username": "ada_l"},
    ],
    "links": [
        {"label": "Twitter", "url": "https://twitter.com/adalovelace"},
        {"label": "Homepage", "url": "https://ada.example.com"},
    ],
}


@respx.mock
def test_gravatar_maps_profile_and_avatar():
    h = gravatar_hash("ada@example.com")
    respx.head(f"https://gravatar.com/avatar/{h}").mock(return_value=httpx.Response(200))
    respx.get(f"https://api.gravatar.com/v3/profiles/{h}").mock(
        return_value=httpx.Response(200, json=GRAVATAR_PROFILE))

    cands = GravatarSource().fetch("ada@example.com")
    by = {c.field: c.value for c in cands}

    assert by["avatar_url"] == f"https://gravatar.com/avatar/{h}"
    assert by["location"] == "London, UK"
    assert by["current_company"] == "Analytic Engines Ltd"
    assert by["current_role"] == "Analytical Engine Programmer"
    assert by["twitter_username"] == "adalovelace"
    assert by["website_url"] == "https://ada.example.com"
    # full_name is the golden key — never mapped
    assert "full_name" not in by
    assert all(c.confidence and c.confidence >= 0.85 for c in cands)


@respx.mock
def test_gravatar_no_profile_404_but_avatar_exists():
    h = gravatar_hash("ada@example.com")
    respx.head(f"https://gravatar.com/avatar/{h}").mock(return_value=httpx.Response(200))
    respx.get(f"https://api.gravatar.com/v3/profiles/{h}").mock(
        return_value=httpx.Response(404))
    cands = GravatarSource().fetch("ada@example.com")
    by = {c.field: c.value for c in cands}
    assert by == {"avatar_url": f"https://gravatar.com/avatar/{h}"}


@respx.mock
def test_gravatar_no_avatar_no_profile_returns_empty():
    h = gravatar_hash("nobody@example.com")
    respx.head(f"https://gravatar.com/avatar/{h}").mock(return_value=httpx.Response(404))
    respx.get(f"https://api.gravatar.com/v3/profiles/{h}").mock(
        return_value=httpx.Response(404))
    assert GravatarSource().fetch("nobody@example.com") == []


@respx.mock
def test_gravatar_rate_limited_returns_empty_not_crash():
    h = gravatar_hash("ada@example.com")
    respx.head(f"https://gravatar.com/avatar/{h}").mock(return_value=httpx.Response(429))
    respx.get(f"https://api.gravatar.com/v3/profiles/{h}").mock(
        return_value=httpx.Response(429))
    assert GravatarSource().fetch("ada@example.com") == []


@respx.mock
def test_gravatar_timeout_returns_empty():
    h = gravatar_hash("ada@example.com")
    respx.head(f"https://gravatar.com/avatar/{h}").mock(side_effect=httpx.ConnectTimeout)
    respx.get(f"https://api.gravatar.com/v3/profiles/{h}").mock(
        side_effect=httpx.ConnectTimeout)
    assert GravatarSource().fetch("ada@example.com") == []


# ----- GitHubSource -----

GITHUB_USER = {
    "login": "adalovelace",
    "company": "@AnalyticEngines",
    "location": "London, UK",
    "blog": "https://ada.example.com",
    "twitter_username": "ada_tweets",
    "avatar_url": "https://avatars.githubusercontent.com/u/1?v=4",
}


@respx.mock
def test_github_noreply_email_parses_username_no_api():
    # id+user form
    respx.get("https://api.github.com/users/adalovelace").mock(
        return_value=httpx.Response(200, json=GITHUB_USER))
    cands = GitHubSource().fetch("1234+adalovelace@users.noreply.github.com")
    by = {c.field: c.value for c in cands}
    assert by["github_username"] == "adalovelace"
    assert by["current_company"] == "@AnalyticEngines"
    assert by["location"] == "London, UK"
    assert by["website_url"] == "https://ada.example.com"
    assert by["twitter_username"] == "ada_tweets"
    assert by["avatar_url"] == "https://github.com/adalovelace.png"


@respx.mock
def test_github_plain_noreply_form():
    respx.get("https://api.github.com/users/adalovelace").mock(
        return_value=httpx.Response(200, json=GITHUB_USER))
    cands = GitHubSource().fetch("adalovelace@users.noreply.github.com")
    by = {c.field: c.value for c in cands}
    assert by["github_username"] == "adalovelace"


def test_github_no_token_no_noreply_skips(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    # plain email, no token → skip entirely (no API), not an error
    assert GitHubSource().fetch("ada@example.com") == []


@respx.mock
def test_github_email_search_requires_total_count_one(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_fake")
    respx.get(url__startswith="https://api.github.com/search/users").mock(
        return_value=httpx.Response(200, json={"total_count": 2, "items": [
            {"login": "a"}, {"login": "b"}]}))
    # ambiguous (fuzzy) → no candidates
    assert GitHubSource().fetch("ada@example.com") == []


@respx.mock
def test_github_email_search_single_hit_uses_login(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_fake")
    respx.get(url__startswith="https://api.github.com/search/users").mock(
        return_value=httpx.Response(200, json={"total_count": 1,
                                               "items": [{"login": "adalovelace"}]}))
    respx.get("https://api.github.com/users/adalovelace").mock(
        return_value=httpx.Response(200, json=GITHUB_USER))
    cands = GitHubSource().fetch("ada@example.com")
    by = {c.field: c.value for c in cands}
    assert by["github_username"] == "adalovelace"


@respx.mock
def test_github_rate_limited_returns_empty(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    respx.get("https://api.github.com/users/adalovelace").mock(
        return_value=httpx.Response(403))
    assert GitHubSource().fetch("adalovelace@users.noreply.github.com") == []


# ----- registry -----

def test_get_source_by_name():
    assert get_source("gravatar").name == "gravatar"
    assert get_source("github").name == "github"
    assert get_source("nope") is None


def test_candidate_is_namedtupleish():
    c = Candidate("location", "SF", 0.9, "https://x")
    assert c.field == "location" and c.value == "SF" and c.confidence == 0.9
