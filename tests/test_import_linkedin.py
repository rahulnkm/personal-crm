import zipfile

from typer.testing import CliRunner

from crm.cli import app

runner = CliRunner()

CONNECTIONS_CSV = """Notes:
"When exporting your connection data, you may notice that some of the email addresses are missing."

First Name,Last Name,URL,Email Address,Company,Position,Connected On
Ada,Lovelace,https://www.linkedin.com/in/ada-l,ada@example.com,Analytical,Engineer,12 Jun 2024
Grace,Hopper,https://www.linkedin.com/in/grace-h,,Navy,Admiral,01 Jan 2020
"""


def _write_zip(tmp_path):
    z = tmp_path / "Basic_LinkedInDataExport.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("Connections.csv", CONNECTIONS_CSV)
    return z


def test_linkedin_zip_stages_people_and_touchpoints(db, tmp_path):
    r = runner.invoke(app, ["import", "linkedin", str(_write_zip(tmp_path))])
    assert r.exit_code == 0, r.output
    people = db.table("staging").select("*").eq("source", "linkedin").execute().data
    assert len(people) == 2
    ada = next(p for p in people if p["full_name"] == "Ada Lovelace")
    assert ada["email"] == "ada@example.com"
    assert ada["linkedin_url"] == "linkedin.com/in/ada-l"
    assert ada["company"] == "Analytical"
    tps = db.table("staging_interactions").select("*").eq("source", "linkedin").execute().data
    assert len(tps) == 2
    assert all(t["kind"] == "origin" and t["channel"] == "linkedin" for t in tps)
    ada_tp = next(t for t in tps if t["linkedin_url"] == "linkedin.com/in/ada-l")
    assert ada_tp["occurred_at"] == "2024-06-12"


def test_linkedin_bare_csv_and_idempotent(db, tmp_path):
    p = tmp_path / "Connections.csv"
    p.write_text(CONNECTIONS_CSV)
    runner.invoke(app, ["import", "linkedin", str(p)])
    runner.invoke(app, ["import", "linkedin", str(p)])
    assert len(db.table("staging").select("id").eq("source", "linkedin").execute().data) == 2
    assert len(db.table("staging_interactions").select("id").eq("source", "linkedin").execute().data) == 2


def test_linkedin_missing_file_fails_cleanly(db):
    r = runner.invoke(app, ["import", "linkedin", "/tmp/nope.zip"])
    assert r.exit_code == 1


def test_linkedin_reexport_with_changed_company_does_not_duplicate_touchpoint(db, tmp_path):
    """A next-year export where a connection changed Company/Position must
    REFRESH the connected-on interaction, not insert a second one — the
    touchpoint digest keys on the stable profile URL, not the row content."""
    p = tmp_path / "Connections.csv"
    p.write_text(CONNECTIONS_CSV)
    runner.invoke(app, ["import", "linkedin", str(p)])
    runner.invoke(app, ["dedup"])
    runner.invoke(app, ["backfill"])
    # re-export: Ada changed companies → person row hash changes
    p.write_text(CONNECTIONS_CSV.replace("Analytical,Engineer", "Babbage Inc,CTO"))
    runner.invoke(app, ["import", "linkedin", str(p)])
    runner.invoke(app, ["dedup"])
    runner.invoke(app, ["backfill"])
    contacts = db.table("contacts").select("id,full_name").ilike(
        "full_name", "Ada Lovelace").execute().data
    assert len(contacts) == 1                      # person folded via linkedin_url
    inter = (db.table("interactions").select("id,kind")
             .eq("contact_id", contacts[0]["id"]).eq("source", "linkedin")
             .execute().data)
    assert len(inter) == 1                         # refreshed, NOT duplicated
