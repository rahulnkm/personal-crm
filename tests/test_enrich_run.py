"""`crm enrich run` — the source-fetch → RPC driver.

Sources are mocked via a FakeSource injected into the registry, so these tests run
with ZERO network. Provenance/survivorship is exercised through the real RPC against
the local stack.
"""
import json

import pytest
from typer.testing import CliRunner

from crm.cli import app
from crm.sources import Candidate

runner = CliRunner()


class FakeSource:
    """Deterministic source returning canned candidates regardless of email."""

    def __init__(self, name, candidates, produces=None):
        self.name = name
        self._candidates = candidates
        self.produces = produces or {c.field for c in candidates}
        self.calls = []

    def fetch(self, email):
        self.calls.append(email)
        return list(self._candidates)


@pytest.fixture()
def patch_sources(monkeypatch):
    """Replace the live registry with a single FakeSource. Returns the source so a
    test can inspect .calls."""
    def install(source):
        import crm.sources as sources_mod
        import crm.commands.enrich as enrich_mod
        registry = [source]
        monkeypatch.setattr(sources_mod, "SOURCES", registry, raising=False)
        monkeypatch.setattr(sources_mod, "_BY_NAME", {source.name: source}, raising=False)
        # the run command imports select_sources/SOURCES from crm.sources at call time
        monkeypatch.setattr(enrich_mod, "select_sources",
                            lambda names: registry, raising=False)
        return source
    return install


def _seed_contact(db, email="ada@example.com", **cols):
    base = {"full_name": "Ada Run", "connection_status": "in_network",
            "closeness_tier": "t1_irl_messaging"}
    base.update(cols)
    c = db.table("contacts").insert(base).execute().data[0]
    if email:
        db.table("contact_identities").insert({
            "contact_id": c["id"], "source": "test", "source_external_id": email,
            "email": email}).execute()
    return c


def test_run_writes_provenance_via_rpc(db, patch_sources):
    c = _seed_contact(db, current_company=None, location=None)
    patch_sources(FakeSource("gravatar", [
        Candidate("current_company", "Analytic Engines", 0.9, "https://gravatar/x"),
        Candidate("location", "London, UK", 0.9, "https://gravatar/x"),
    ]))
    r = runner.invoke(app, ["enrich", "run", "--json"])
    assert r.exit_code == 0, r.output

    got = db.table("contacts").select("current_company,location,last_enriched_at") \
        .eq("id", c["id"]).single().execute().data
    assert got["current_company"] == "Analytic Engines"
    assert got["location"] == "London, UK"
    assert got["last_enriched_at"] is not None

    # provenance row exists with the source name + method
    log = db.table("enrichment_log").select("field,source,method,source_detail") \
        .eq("contact_id", c["id"]).eq("field", "current_company").execute().data
    assert log and log[0]["source"] == "gravatar"
    assert log[0]["method"] == "enrich_api"
    assert log[0]["source_detail"] == "https://gravatar/x"


def test_run_dry_run_writes_nothing(db, patch_sources):
    c = _seed_contact(db, current_company=None)
    patch_sources(FakeSource("gravatar", [
        Candidate("current_company", "Analytic Engines", 0.9, "https://x")]))
    r = runner.invoke(app, ["enrich", "run", "--dry-run", "--json"])
    assert r.exit_code == 0, r.output
    got = db.table("contacts").select("current_company,last_enriched_at") \
        .eq("id", c["id"]).single().execute().data
    assert got["current_company"] is None
    assert got["last_enriched_at"] is None
    # no provenance written
    assert db.table("enrichment_log").select("id").eq("contact_id", c["id"]) \
        .execute().data == []


def test_run_only_missing_skips_already_enriched(db, patch_sources):
    # last_enriched_at set → skipped by --only-missing (default)
    c = _seed_contact(db, current_company=None, last_enriched_at="2026-01-01")
    src = patch_sources(FakeSource("gravatar", [
        Candidate("current_company", "X", 0.9, "https://x")]))
    r = runner.invoke(app, ["enrich", "run", "--json"])
    assert r.exit_code == 0, r.output
    # source never called for the skipped contact
    assert src.calls == []
    got = db.table("contacts").select("current_company") \
        .eq("id", c["id"]).single().execute().data
    assert got["current_company"] is None


def test_run_no_only_missing_reenriches(db, patch_sources):
    c = _seed_contact(db, current_company=None, last_enriched_at="2026-01-01")
    src = patch_sources(FakeSource("gravatar", [
        Candidate("current_company", "X", 0.9, "https://x")]))
    r = runner.invoke(app, ["enrich", "run", "--no-only-missing", "--json"])
    assert r.exit_code == 0, r.output
    assert src.calls == ["ada@example.com"]
    got = db.table("contacts").select("current_company") \
        .eq("id", c["id"]).single().execute().data
    assert got["current_company"] == "X"


def test_run_only_missing_skips_when_all_fields_present(db, patch_sources):
    # not enriched yet, but already has the one field the source produces → skip
    c = _seed_contact(db, current_company="Existing Co", last_enriched_at=None)
    src = patch_sources(FakeSource("gravatar", [
        Candidate("current_company", "New Co", 0.9, "https://x")],
        produces={"current_company"}))
    r = runner.invoke(app, ["enrich", "run", "--json"])
    assert r.exit_code == 0, r.output
    assert src.calls == []  # nothing missing → not fetched


def test_run_status_filter_excludes_non_in_network(db, patch_sources):
    _seed_contact(db, full_name="In Net", connection_status="in_network",
                  current_company=None)
    off = _seed_contact(db, full_name="Off File", connection_status="contact_on_file",
                        current_company=None, email="off@example.com")
    src = patch_sources(FakeSource("gravatar", [
        Candidate("current_company", "X", 0.9, "https://x")]))
    r = runner.invoke(app, ["enrich", "run", "--json"])
    assert r.exit_code == 0, r.output
    # default --status in_network → off-file contact's email never fetched
    assert "off@example.com" not in src.calls
    assert db.table("contacts").select("current_company").eq("id", off["id"]) \
        .single().execute().data["current_company"] is None


def test_run_contact_without_email_reports_no_email(db, patch_sources):
    _seed_contact(db, email=None, current_company=None)
    patch_sources(FakeSource("gravatar", [
        Candidate("current_company", "X", 0.9, "https://x")]))
    r = runner.invoke(app, ["enrich", "run", "--json"])
    assert r.exit_code == 0, r.output
    out = json.loads(r.output)
    statuses = {row["status"] for row in out["contacts"]}
    assert "no_email" in statuses


def test_run_json_summary_shape(db, patch_sources):
    _seed_contact(db, current_company=None)
    patch_sources(FakeSource("gravatar", [
        Candidate("current_company", "X", 0.9, "https://x")]))
    r = runner.invoke(app, ["enrich", "run", "--json"])
    out = json.loads(r.output)
    assert "summary" in out and "contacts" in out
    assert out["summary"]["enriched_fields"] >= 1
    row = out["contacts"][0]
    for k in ("contact_id", "name", "status", "fields"):
        assert k in row


def test_run_limit_caps_contacts(db, patch_sources):
    for i in range(3):
        _seed_contact(db, full_name=f"P{i}", email=f"p{i}@example.com",
                      current_company=None)
    patch_sources(FakeSource("gravatar", [
        Candidate("current_company", "X", 0.9, "https://x")]))
    r = runner.invoke(app, ["enrich", "run", "--limit", "1", "--json"])
    assert r.exit_code == 0, r.output
    assert len(json.loads(r.output)["contacts"]) == 1
