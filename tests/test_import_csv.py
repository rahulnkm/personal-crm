import csv

from typer.testing import CliRunner

from crm.cli import app

runner = CliRunner()


def make_csv(tmp_path, rows, headers):
    p = tmp_path / "list.csv"
    with open(p, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=headers)
        w.writeheader()
        w.writerows(rows)
    return p


def test_import_csv_to_staging(db, tmp_path):
    p = make_csv(
        tmp_path,
        [{"Name": "Ada Lovelace", "Email": "ADA@example.com", "Firm": "Analytical"}],
        ["Name", "Email", "Firm"],
    )
    r = runner.invoke(app, [
        "import", "csv", str(p), "--source", "csv_test",
        "--map", "full_name=Name,email=Email,company=Firm",
    ])
    assert r.exit_code == 0, r.output
    staged = db.table("staging").select("*").eq("source", "csv_test").execute().data
    assert len(staged) == 1
    assert staged[0]["email"] == "ada@example.com"   # normalized
    assert staged[0]["match_status"] == "pending"
    assert staged[0]["raw_json"]["Email"] == "ADA@example.com"  # raw preserved


def test_reimport_is_idempotent(db, tmp_path):
    p = make_csv(tmp_path, [{"Name": "Ada", "Email": "a@b.co"}], ["Name", "Email"])
    args = ["import", "csv", str(p), "--source", "csv_test",
            "--map", "full_name=Name,email=Email"]
    runner.invoke(app, args)
    runner.invoke(app, args)  # re-run: same rows, no duplicates
    staged = db.table("staging").select("id").eq("source", "csv_test").execute().data
    assert len(staged) == 1


def test_unknown_map_column_fails_cleanly(db, tmp_path):
    p = make_csv(tmp_path, [{"Name": "Ada"}], ["Name"])
    r = runner.invoke(app, ["import", "csv", str(p), "--source", "x",
                            "--map", "full_name=Nope"])
    assert r.exit_code == 1


def test_missing_file_fails_cleanly(db):
    r = runner.invoke(app, ["import", "csv", "/tmp/does-not-exist-xyz.csv",
                            "--source", "x", "--map", "full_name=Name"])
    assert r.exit_code == 1


def test_malformed_map_fails_cleanly(db, tmp_path):
    p = make_csv(tmp_path, [{"Name": "Ada"}], ["Name"])
    r = runner.invoke(app, ["import", "csv", str(p), "--source", "x",
                            "--map", "full_name"])
    assert r.exit_code == 1


def test_first_last_name_composed_to_full_name(db, tmp_path):
    p = make_csv(
        tmp_path,
        [{"First Name": "Ada", "Last Name": "Lovelace", "Company": "Analytical"}],
        ["First Name", "Last Name", "Company"],
    )
    r = runner.invoke(app, [
        "import", "csv", str(p), "--source", "csv_firstlast",
        "--map", "first_name=First Name,last_name=Last Name,company=Company",
    ])
    assert r.exit_code == 0, r.output
    staged = db.table("staging").select("*").eq("source", "csv_firstlast").execute().data
    assert len(staged) == 1
    assert staged[0]["full_name"] == "Ada Lovelace"
    # first_name / last_name must not bleed into staging (no such columns)
    assert "first_name" not in staged[0]
    assert "last_name" not in staged[0]


def test_social_columns_map_and_normalize(db, tmp_path):
    p = make_csv(
        tmp_path,
        [{"Name": "Ada Lovelace", "Twitter": "https://x.com/AdaLovelace",
          "GitHub": "@AdaLovelace", "Site": "https://ada.example.com"}],
        ["Name", "Twitter", "GitHub", "Site"],
    )
    r = runner.invoke(app, [
        "import", "csv", str(p), "--source", "csv_social",
        "--map", "full_name=Name,twitter_username=Twitter,"
                 "github_username=GitHub,website_url=Site",
    ])
    assert r.exit_code == 0, r.output
    staged = db.table("staging").select("*").eq("source", "csv_social").execute().data
    assert len(staged) == 1
    assert staged[0]["twitter_username"] == "adalovelace"   # URL → bare handle
    assert staged[0]["github_username"] == "adalovelace"    # @ stripped
    assert staged[0]["website_url"] == "https://ada.example.com"  # kept as-is


def test_dedup_promotes_socials_to_new_contact(db, tmp_path):
    # no match → bulk create path (create_contacts_with_identities RPC)
    p = make_csv(
        tmp_path,
        [{"Name": "Ada Lovelace", "Twitter": "adalovelace",
          "GitHub": "ada-lovelace", "Site": "https://ada.example.com"}],
        ["Name", "Twitter", "GitHub", "Site"],
    )
    runner.invoke(app, [
        "import", "csv", str(p), "--source", "csv_social",
        "--map", "full_name=Name,twitter_username=Twitter,"
                 "github_username=GitHub,website_url=Site",
    ])
    r = runner.invoke(app, ["dedup"])
    assert r.exit_code == 0, r.output
    contacts = db.table("contacts").select("*").execute().data
    assert len(contacts) == 1
    assert contacts[0]["twitter_username"] == "adalovelace"
    assert contacts[0]["github_username"] == "ada-lovelace"
    assert contacts[0]["website_url"] == "https://ada.example.com"


def test_dedup_fills_socials_on_existing_contact(db, tmp_path):
    # attach path (FILL_FIELDS): existing contact's null socials get filled
    p1 = make_csv(tmp_path, [{"Name": "Ada Lovelace", "Email": "ada@example.com"}],
                  ["Name", "Email"])
    runner.invoke(app, ["import", "csv", str(p1), "--source", "s1",
                        "--map", "full_name=Name,email=Email"])
    runner.invoke(app, ["dedup"])
    p2 = make_csv(tmp_path,
                  [{"Name": "Ada Lovelace", "Email": "ada@example.com",
                    "Twitter": "https://twitter.com/AdaLovelace"}],
                  ["Name", "Email", "Twitter"])
    runner.invoke(app, ["import", "csv", str(p2), "--source", "s2",
                        "--map", "full_name=Name,email=Email,twitter_username=Twitter"])
    r = runner.invoke(app, ["dedup"])
    assert r.exit_code == 0, r.output
    contacts = db.table("contacts").select("*").execute().data
    assert len(contacts) == 1                            # attached, not duplicated
    assert contacts[0]["twitter_username"] == "adalovelace"


def test_review_reject_create_carries_socials(db):
    # _create (review --reject path) hardcodes its field list — must carry socials
    from crm.commands.dedup import _create
    row = db.table("staging").insert(
        {"source": "s1", "source_external_id": "h1", "full_name": "Ada Lovelace",
         "twitter_username": "adalovelace", "github_username": "ada-lovelace",
         "website_url": "https://ada.example.com"}).execute().data[0]
    cid = _create(db, row)
    c = db.table("contacts").select("*").eq("id", cid).single().execute().data
    assert c["twitter_username"] == "adalovelace"
    assert c["github_username"] == "ada-lovelace"
    assert c["website_url"] == "https://ada.example.com"
