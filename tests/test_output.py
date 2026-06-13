import json

from crm.output import render


def test_render_json_mode(capsys):
    render([{"a": 1}], as_json=True)
    out = capsys.readouterr().out
    assert json.loads(out) == [{"a": 1}]


def test_render_table_mode(capsys):
    render([{"name": "Ada", "company": "X"}], as_json=False)
    out = capsys.readouterr().out
    assert "Ada" in out and "name" in out


def test_render_empty(capsys):
    render([], as_json=False)
    assert "0 rows" in capsys.readouterr().out
