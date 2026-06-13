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
