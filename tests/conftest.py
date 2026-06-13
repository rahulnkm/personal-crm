"""DB-backed tests run ONLY against the local Supabase stack (supabase start).

The fixture truncates data tables between tests so each test is isolated.
If the local stack isn't reachable, DB tests skip with instructions.

Post-cloud-cutover: ~/.crm/.env holds the CLOUD credentials, so tests must
source BOTH the URL and the secret key from the project-root ./.env.local (local
stack creds, gitignored) — forcing only the URL would pair the local API
with the cloud key and fail auth.
"""
import os
from pathlib import Path

import httpx
import pytest
from dotenv import dotenv_values

LOCAL_URL = "http://127.0.0.1:54321"
LOCAL_ENV = Path(__file__).resolve().parent.parent / ".env.local"

DATA_TABLES = [
    "staging_interactions",
    "enrichment_log", "interactions", "events", "staging",
    "contact_identities", "contacts", "tag_registry",
]


def _local_stack_up() -> bool:
    try:
        httpx.get(LOCAL_URL + "/rest/v1/", timeout=2)
        return True
    except Exception:
        return False


@pytest.fixture()
def db():
    if os.environ.get("SUPABASE_URL", LOCAL_URL) != LOCAL_URL:
        pytest.skip("Refusing to run DB tests against a non-local SUPABASE_URL")
    if not _local_stack_up():
        pytest.skip("Local Supabase not running — `supabase start` first")
    local = dotenv_values(LOCAL_ENV) if LOCAL_ENV.exists() else {}
    if local.get("SUPABASE_URL") != LOCAL_URL or not local.get("SUPABASE_SECRET_KEY"):
        pytest.skip("./.env.local must hold LOCAL stack creds — regenerate with: "
                    "supabase status -o env | grep -E '^(API_URL|SERVICE_ROLE_KEY)' "
                    "| sed 's/^API_URL=/SUPABASE_URL=/; s/^SERVICE_ROLE_KEY=/SUPABASE_SECRET_KEY=/' "
                    "| tr -d '\"' > .env.local")
    original = {k: os.environ.get(k) for k in ("SUPABASE_URL", "SUPABASE_SECRET_KEY")}
    os.environ["SUPABASE_URL"] = LOCAL_URL
    os.environ["SUPABASE_SECRET_KEY"] = local["SUPABASE_SECRET_KEY"]
    from crm.config import get_client

    client = get_client()
    for t in DATA_TABLES:  # clean slate; keep seeded 'rahul' agent
        client.table(t).delete().neq(
            "tag" if t == "tag_registry" else "id",
            "___none___" if t == "tag_registry" else "00000000-0000-0000-0000-000000000000",
        ).execute()
    yield client
    for k, v in original.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
