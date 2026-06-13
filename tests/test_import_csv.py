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
