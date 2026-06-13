import sqlite3

from typer.testing import CliRunner

from crm.cli import app

runner = CliRunner()


def make_abcddb(tmp_path):
    """Minimal AddressBook-shaped sqlite fixture."""
    p = tmp_path / "AddressBook-v22.abcddb"
    con = sqlite3.connect(p)
    con.executescript("""
        create table ZABCDRECORD (Z_PK integer primary key, ZFIRSTNAME text,
                                  ZLASTNAME text, ZORGANIZATION text);
        create table ZABCDPHONENUMBER (Z_PK integer primary key, ZFULLNUMBER text,
                                       ZOWNER integer);
        create table ZABCDEMAILADDRESS (Z_PK integer primary key, ZADDRESS text,
                                        ZOWNER integer);
        insert into ZABCDRECORD values (1, 'Ada', 'Lovelace', 'Analytical');
        insert into ZABCDRECORD values (2, 'Grace', 'Hopper', null);
        insert into ZABCDRECORD values (3, null, null, 'Just A Company');
        insert into ZABCDPHONENUMBER values (1, '(415) 555-2671', 1);
        insert into ZABCDEMAILADDRESS values (1, 'ADA@example.com', 1);
        insert into ZABCDEMAILADDRESS values (2, 'grace@navy.mil', 2);
    """)
    con.commit(); con.close()
    return p


def test_apple_contacts_same_number_twice_does_not_crash(db, tmp_path):
    """Real abcddbs store the same phone twice (two formats → one E.164),
    producing duplicate (source, source_external_id) digests. They must be
    collapsed before the batch upsert, or Postgres raises 21000."""
    p = tmp_path / "AddressBook-v22.abcddb"
    con = sqlite3.connect(p)
    con.executescript("""
        create table ZABCDRECORD (Z_PK integer primary key, ZFIRSTNAME text,
                                  ZLASTNAME text, ZORGANIZATION text);
        create table ZABCDPHONENUMBER (Z_PK integer primary key, ZFULLNUMBER text,
                                       ZOWNER integer);
        create table ZABCDEMAILADDRESS (Z_PK integer primary key, ZADDRESS text,
                                        ZOWNER integer);
        insert into ZABCDRECORD values (1, 'Ada', 'Lovelace', null);
        insert into ZABCDPHONENUMBER values (1, '(415) 555-2671', 1);
        insert into ZABCDPHONENUMBER values (2, '+1 415-555-2671', 1);
    """)
    con.commit(); con.close()
    r = runner.invoke(app, ["import", "apple-contacts", "--db", str(p)])
    assert r.exit_code == 0, r.output
    rows = db.table("staging").select("*").eq("source", "apple_contacts").execute().data
    assert len(rows) == 1                           # collapsed, not crashed
    assert rows[0]["phone"] == "+14155552671"


def test_apple_contacts_one_row_per_contact_point(db, tmp_path):
    fixture = make_abcddb(tmp_path)
    r = runner.invoke(app, ["import", "apple-contacts", "--db", str(fixture)])
    assert r.exit_code == 0, r.output
    rows = db.table("staging").select("*").eq("source", "apple_contacts").execute().data
    ada = [x for x in rows if x["full_name"] == "Ada Lovelace"]
    assert len(ada) == 2                       # one email row + one phone row
    assert {a["email"] or a["phone"] for a in ada} == {"ada@example.com", "+14155552671"}
    assert all(a["company"] == "Analytical" for a in ada)
    names = {x["full_name"] for x in rows}
    assert "Grace Hopper" in names
    assert len(rows) == 3                      # nameless org-only record skipped


def test_apple_contacts_dedup_folds_rows(db, tmp_path):
    fixture = make_abcddb(tmp_path)
    runner.invoke(app, ["import", "apple-contacts", "--db", str(fixture)])
    r = runner.invoke(app, ["dedup"])
    assert r.exit_code == 0
    contacts = db.table("contacts").select("full_name").execute().data
    assert sorted(c["full_name"] for c in contacts) == ["Ada Lovelace", "Grace Hopper"]
    idents = db.table("contact_identities").select("id").execute().data
    assert len(idents) == 3


def test_apple_contacts_missing_db_fails_with_fda_hint(db):
    r = runner.invoke(app, ["import", "apple-contacts", "--db", "/tmp/nope.abcddb"])
    assert r.exit_code == 1
