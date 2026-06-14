import json

from typer.testing import CliRunner

from crm.cli import app

runner = CliRunner()


def _seed(db, name="Ada Lovelace", **kw):
    return db.table("contacts").insert({"full_name": name, **kw}).execute().data[0]


def test_add_and_show_contact(db):
    r = runner.invoke(app, ["add", "Grace Hopper", "--status", "in_network",
                            "--affiliation", "rutgers", "--agent", "rahul"])
    assert r.exit_code == 0, r.output
    r = runner.invoke(app, ["contact", "Grace Hopper", "--json"])
    data = json.loads(r.output)
    assert data["contact"]["connection_status"] == "in_network"
    assert "rutgers" in data["contact"]["affiliations"]


def test_list_filters_status_and_tier(db):
    _seed(db, "A In", connection_status="in_network", closeness_tier="t1_irl_messaging")
    _seed(db, "B File", connection_status="contact_on_file")
    r = runner.invoke(app, ["list", "--status", "in_network", "--json"])
    rows = json.loads(r.output)
    assert [x["full_name"] for x in rows] == ["A In"]
    r = runner.invoke(app, ["list", "--tier", "t1_irl_messaging", "--json"])
    assert len(json.loads(r.output)) == 1


def test_list_filters_by_role(db):
    _seed(db, "Carol Founder", current_role="Founder & CEO", connection_status="in_network")
    _seed(db, "Dan Eng", current_role="Staff Engineer", connection_status="in_network")
    # case-insensitive substring on current_role
    r = runner.invoke(app, ["list", "--role", "FOUNDER", "--json"])
    rows = json.loads(r.output)
    assert [x["full_name"] for x in rows] == ["Carol Founder"]
    # composes with --status (AND): the contact_on_file founder is excluded
    _seed(db, "Eve Founder", current_role="Founder", connection_status="contact_on_file")
    r = runner.invoke(app, ["list", "--role", "founder", "--status", "in_network", "--json"])
    rows = json.loads(r.output)
    assert [x["full_name"] for x in rows] == ["Carol Founder"]


def test_list_role_escapes_wildcards(db):
    # a literal % in the query must NOT act as a SQL wildcard
    _seed(db, "Frank Founder", current_role="Founder", connection_status="in_network")
    r = runner.invoke(app, ["list", "--role", "f%under", "--json"])
    assert r.exit_code == 0, r.output
    assert json.loads(r.output) == []


def test_set_validates_field_and_updates(db):
    c = _seed(db)
    r = runner.invoke(app, ["set", c["id"], "connection_status=in_network"])
    assert r.exit_code == 0
    r = runner.invoke(app, ["set", c["id"], "bogus_field=x"])
    assert r.exit_code == 1
    r = runner.invoke(app, ["set", c["id"], "tags=fundraising"])
    assert r.exit_code == 1  # tag not in registry yet
    runner.invoke(app, ["tags", "add", "fundraising", "--desc", "raising a round"])
    r = runner.invoke(app, ["set", c["id"], "tags=fundraising"])
    assert r.exit_code == 0


def test_set_last_enriched_at(db):
    c = _seed(db)
    r = runner.invoke(app, ["set", c["id"], "last_enriched_at=2026-06-14"])
    assert r.exit_code == 0, r.output
    row = (db.table("contacts").select("last_enriched_at")
           .eq("id", c["id"]).single().execute().data)
    assert row["last_enriched_at"] == "2026-06-14"
    log = (db.table("enrichment_log").select("field")
           .eq("contact_id", c["id"]).execute().data)
    assert any(e["field"] == "last_enriched_at" for e in log)


def test_set_last_enriched_at_rejects_malformed(db):
    # date column → Postgres rejects a non-date value (does not store it)
    c = _seed(db)
    r = runner.invoke(app, ["set", c["id"], "last_enriched_at=banana"])
    assert r.exit_code != 0
    row = (db.table("contacts").select("last_enriched_at")
           .eq("id", c["id"]).single().execute().data)
    assert row["last_enriched_at"] is None


def test_search_fuzzy(db):
    _seed(db, "Devendra Bhatt", current_company="Cedar Capital")
    r = runner.invoke(app, ["search", "devendra bhat", "--json"])
    assert "Devendra Bhatt" in r.output


def test_note_appends(db):
    c = _seed(db)
    runner.invoke(app, ["note", c["id"], "met at NS in Forest City"])
    runner.invoke(app, ["note", c["id"], "wants help with a fundraise"])
    row = db.table("contacts").select("notes").eq("id", c["id"]).single().execute().data
    assert "Forest City" in row["notes"] and "fundraise" in row["notes"]


def test_search_with_comma_does_not_crash(db):
    _seed(db, "Probe Comma", current_company="Anderson, Inc")
    r = runner.invoke(app, ["search", "Anderson, Inc", "--json"])
    assert r.exit_code == 0


def test_set_invalid_enum_fails_cleanly(db):
    c = _seed(db)
    r = runner.invoke(app, ["set", c["id"], "connection_status=bogus"])
    assert r.exit_code == 1
    r = runner.invoke(app, ["add", "X Y", "--status", "nonsense"])
    assert r.exit_code == 1


def test_add_rolls_back_orphan_when_identity_insert_fails(db, monkeypatch):
    """If the contact is created but the identity insert fails, add() must NOT
    report success — it rolls back the orphan contact and exits non-zero, so a
    scripted caller can tell the create half-failed."""
    from crm.commands import contacts as contacts_mod

    real = contacts_mod.get_client()  # db fixture has pointed env at the local stack

    class _RaisingExec:
        def execute(self):
            raise RuntimeError("simulated identity insert failure")

    class _RaisingIdentities:
        def insert(self, *a, **k):
            return _RaisingExec()

    class _Proxy:
        """Delegates everything to the real client except contact_identities
        inserts, which fail — simulating a DB error on the second write."""
        def table(self, name):
            return _RaisingIdentities() if name == "contact_identities" else real.table(name)
        def __getattr__(self, name):
            return getattr(real, name)

    monkeypatch.setattr(contacts_mod, "get_client", lambda: _Proxy())

    r = runner.invoke(app, ["add", "Orphan Probe", "--agent", "rahul"])
    assert r.exit_code == 1, r.output
    left = real.table("contacts").select("id").eq("full_name", "Orphan Probe").execute().data
    assert left == [], "orphan contact must be rolled back, not left behind"
