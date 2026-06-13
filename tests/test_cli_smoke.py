from typer.testing import CliRunner
from crm.cli import app

runner = CliRunner()

def test_version_exits_zero():
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert result.output.strip().startswith("crm ")
    assert "0.1.0" in result.output
