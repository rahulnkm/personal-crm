# tests/test_import_touchpoints.py
import csv

from typer.testing import CliRunner

from crm.cli import app

runner = CliRunner()


def make_csv(tmp_path, rows, headers):
    p = tmp_path / "touch.csv"
    with open(p, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=headers)
        w.writeheader()
        w.writerows(rows)
    return p


def test_touchpoints_csv_to_staging(db, tmp_path):
    p = make_csv(
        tmp_path,
        [{"Email": "ADA@example.com", "Date": "2026-05-20",
          "What": "talked agents", "Event": "NS dinner"}],
        ["Email", "Date", "What", "Event"],
    )
    r = runner.invoke(app, [
        "import", "touchpoints", str(p), "--source", "tp_test",
        "--map", "email=Email,occurred_at=Date,summary=What,event_name=Event",
        "--kind", "event", "--channel", "irl",
    ])
    assert r.exit_code == 0, r.output
    rows = db.table("staging_interactions").select("*").eq("source", "tp_test").execute().data
    assert len(rows) == 1
    assert rows[0]["email"] == "ada@example.com"      # normalized
    assert rows[0]["kind"] == "event"
    assert rows[0]["channel"] == "irl"
    assert rows[0]["match_status"] == "pending"


def test_touchpoints_reimport_idempotent(db, tmp_path):
    p = make_csv(tmp_path, [{"Email": "a@b.co", "Date": "2026-01-01"}], ["Email", "Date"])
    args = ["import", "touchpoints", str(p), "--source", "tp_test",
            "--map", "email=Email,occurred_at=Date", "--kind", "message",
            "--channel", "telegram"]
    runner.invoke(app, args)
    runner.invoke(app, args)
    rows = db.table("staging_interactions").select("id").eq("source", "tp_test").execute().data
    assert len(rows) == 1


def test_touchpoints_requires_a_match_key(db, tmp_path):
    p = make_csv(tmp_path, [{"Date": "2026-01-01"}], ["Date"])
    r = runner.invoke(app, ["import", "touchpoints", str(p), "--source", "x",
                            "--map", "occurred_at=Date", "--kind", "message",
                            "--channel", "email"])
    assert r.exit_code == 1  # no email/phone/handle/linkedin mapped → unusable


def test_touchpoints_invalid_kind_or_date(db, tmp_path):
    p = make_csv(tmp_path, [{"Email": "a@b.co", "Date": "junk"}], ["Email", "Date"])
    r = runner.invoke(app, ["import", "touchpoints", str(p), "--source", "x",
                            "--map", "email=Email,occurred_at=Date",
                            "--kind", "bogus", "--channel", "email"])
    assert r.exit_code == 1   # invalid kind
    r = runner.invoke(app, ["import", "touchpoints", str(p), "--source", "x",
                            "--map", "email=Email,occurred_at=Date",
                            "--kind", "message", "--channel", "email"])
    assert r.exit_code == 0   # bad per-row date → row skipped, not fatal
    rows = db.table("staging_interactions").select("id").eq("source", "x").execute().data
    assert rows == []
