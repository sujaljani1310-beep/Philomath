import os

from dotenv import load_dotenv
from supabase import Client, create_client

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

# Prefer a server-only service role/secret key. SUPABASE_KEY remains as a
# backwards-compatible fallback for the user's current local setup.
_server_key = SUPABASE_SERVICE_ROLE_KEY or SUPABASE_KEY

if SUPABASE_URL and _server_key:
    supabase: Client = create_client(SUPABASE_URL, _server_key)
else:
    supabase = None


def is_supabase_connected() -> bool:
    return supabase is not None


def require_supabase() -> Client:
    if supabase is None:
        raise RuntimeError(
            "Supabase is not configured. Add SUPABASE_URL and "
            "SUPABASE_SERVICE_ROLE_KEY to the backend environment."
        )

    return supabase
