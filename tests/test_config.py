import pytest
from crm.config import load_settings


def test_load_settings_from_env(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "http://localhost:54321")
    monkeypatch.setenv("SUPABASE_SECRET_KEY", "sk-test")
    s = load_settings()
    assert s.url == "http://localhost:54321"
    assert s.secret_key == "sk-test"


def test_missing_env_raises(monkeypatch):
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_SECRET_KEY", raising=False)
    monkeypatch.setattr("crm.config.ENV_PATHS", [])  # ignore real .env files
    with pytest.raises(RuntimeError, match="SUPABASE_URL"):
        load_settings()
