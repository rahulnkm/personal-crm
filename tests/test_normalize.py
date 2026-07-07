from crm.normalize import (
    normalize_email,
    normalize_linkedin,
    normalize_name,
    normalize_phone,
)


def test_email_lowercased_and_stripped():
    assert normalize_email("  Test.User@Example.COM ") == "test.user@example.com"
    assert normalize_email("") is None
    assert normalize_email(None) is None
    assert normalize_email("not-an-email") is None  # must contain @


def test_phone_to_e164_default_us():
    assert normalize_phone("(415) 555-2671") == "+14155552671"
    assert normalize_phone("415.555.2671") == "+14155552671"
    assert normalize_phone("+44 20 7946 0958") == "+442079460958"
    assert normalize_phone("12") is None        # unparseable
    assert normalize_phone(None) is None


def test_linkedin_url_canonical():
    assert (
        normalize_linkedin("HTTPS://www.LinkedIn.com/in/Rahul-N/?utm=x")
        == "linkedin.com/in/rahul-n"
    )
    assert normalize_linkedin("linkedin.com/in/rahul-n") == "linkedin.com/in/rahul-n"
    assert normalize_linkedin("https://example.com/in/x") is None
    assert normalize_linkedin(None) is None


def test_name_casefold_accents_whitespace():
    assert normalize_name("  José   GARCÍA ") == "jose garcia"
    assert normalize_name("O'Brien, Tim") == "o'brien, tim"
    assert normalize_name(None) is None


def test_email_rejects_free_text_with_at():
    assert normalize_email("see notes @ slide 3") is None
    assert normalize_email("a@") is None
    assert normalize_email("a@b") is None          # no dot in domain
    assert normalize_email("a@b.co") == "a@b.co"


def test_linkedin_rejects_non_person_urls():
    assert normalize_linkedin("https://linkedin.com/company/acme") is None
    assert normalize_linkedin("https://linkedin.com/in/") is None
    assert normalize_linkedin("https://linkedin.com/in") is None


def test_linkedin_mobile_port_and_encoding():
    assert normalize_linkedin("https://m.linkedin.com/in/rahul-n") == "linkedin.com/in/rahul-n"
    assert normalize_linkedin("https://linkedin.com:443/in/rahul-n") == "linkedin.com/in/rahul-n"
    assert normalize_linkedin("https://linkedin.com/in/rahul%2Dn") == "linkedin.com/in/rahul-n"


def test_twitter_handle_forms():
    from crm.normalize import normalize_twitter
    assert normalize_twitter("https://x.com/AdaLovelace") == "adalovelace"
    assert normalize_twitter("https://twitter.com/AdaLovelace/") == "adalovelace"
    assert normalize_twitter("www.twitter.com/AdaLovelace") == "adalovelace"
    assert normalize_twitter("@AdaLovelace") == "adalovelace"
    assert normalize_twitter("AdaLovelace") == "adalovelace"
    assert normalize_twitter("https://x.com/AdaLovelace/status/123") == "adalovelace"
    assert normalize_twitter("https://x.com/i/status/123") is None       # reserved route
    assert normalize_twitter("https://twitter.com/intent/follow?screen_name=ada") is None
    assert normalize_twitter("https://example.com/ada") is None  # wrong host
    assert normalize_twitter("not a handle!") is None            # bad charset
    assert normalize_twitter(None) is None


def test_github_handle_forms():
    from crm.normalize import normalize_github
    assert normalize_github("https://github.com/AdaLovelace") == "adalovelace"
    assert normalize_github("github.com/Ada-Lovelace") == "ada-lovelace"
    assert normalize_github("@AdaLovelace") == "adalovelace"
    assert normalize_github("ada-lovelace") == "ada-lovelace"
    assert normalize_github("a-b") == "a-b"
    assert normalize_github("a" * 39) == "a" * 39                # at the 39-char cap
    assert normalize_github("https://github.com/orgs/anthropics") is None  # reserved route
    assert normalize_github("ada-") is None                      # trailing hyphen
    assert normalize_github("a--b") is None                      # consecutive hyphens
    assert normalize_github("a" * 40) is None                    # over the cap
    assert normalize_github("https://gitlab.com/ada") is None    # wrong host
    assert normalize_github(None) is None
