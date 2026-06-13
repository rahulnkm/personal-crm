"""Settings + Supabase client factory.

The CLI is installed globally and runs from any cwd, so .env is loaded from a
fixed path first (~/.crm/.env), then ./.env as a dev fallback. Plain env vars
always win (dotenv does not override them).
"""
import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv
from supabase import Client, create_client

ENV_PATHS = [Path.home() / ".crm" / ".env", Path(".env")]


@dataclass(frozen=True)
class Settings:
    url: str
    secret_key: str = field(repr=False)


def load_settings() -> Settings:
    for p in ENV_PATHS:
        if p.exists():
            load_dotenv(p, override=False)
    url = (os.environ.get("SUPABASE_URL") or "").strip()
    key = (os.environ.get("SUPABASE_SECRET_KEY") or "").strip()
    if not url or not key:
        raise RuntimeError(
            "SUPABASE_URL / SUPABASE_SECRET_KEY not set. "
            "Copy .env.example to ~/.crm/.env and fill it in."
        )
    return Settings(url=url, secret_key=key)


def get_client() -> Client:
    s = load_settings()
    return create_client(s.url, s.secret_key)
