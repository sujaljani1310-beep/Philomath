import os
from typing import Dict, List

from cryptography.fernet import Fernet, InvalidToken

from app.observability import timed
from app.services.persistence import (
    PersistenceUnavailable, affected_rows, database_operation, mutation_guard,
)
from app.services.supabase_service import require_supabase


PROVIDER_ALIASES = {
    "openrouter": "openrouter",
    "open router": "openrouter",
    "cerebras": "cerebras",
    "nvidia": "nvidia",
    "nvidia nim": "nvidia",
    "nim": "nvidia",
    "gemini": "gemini",
    "google gemini": "gemini",
    "google": "gemini",
    "grok": "grok",
    "xai": "grok",
    "x.ai": "grok",
}

PROVIDER_DISPLAY_NAMES = {
    "openrouter": "OpenRouter",
    "cerebras": "Cerebras",
    "nvidia": "NVIDIA",
    "gemini": "Gemini",
    "grok": "Grok",
}


def normalize_provider_name(name: str) -> str:
    normalized = " ".join((name or "").strip().lower().split())
    provider = PROVIDER_ALIASES.get(normalized)

    if provider is None:
        supported = ", ".join(PROVIDER_DISPLAY_NAMES.values())
        raise ValueError(
            f"Unsupported AI name '{name}'. Supported AIs: {supported}."
        )

    return provider


def _fernet() -> Fernet:
    raw_key = os.getenv("PHILOMATH_ENCRYPTION_KEY", "").strip()

    if not raw_key:
        raise RuntimeError(
            "PHILOMATH_ENCRYPTION_KEY is missing from the backend environment."
        )

    try:
        return Fernet(raw_key.encode("utf-8"))
    except Exception as error:
        raise RuntimeError(
            "PHILOMATH_ENCRYPTION_KEY is invalid. Generate a Fernet key."
        ) from error


def _encrypt_api_key(api_key: str) -> str:
    return _fernet().encrypt(api_key.encode("utf-8")).decode("utf-8")


def _decrypt_api_key(encrypted_api_key: str) -> str:
    try:
        return _fernet().decrypt(
            encrypted_api_key.encode("utf-8")
        ).decode("utf-8")
    except InvalidToken as error:
        raise RuntimeError(
            "A saved AI key could not be decrypted. Check PHILOMATH_ENCRYPTION_KEY."
        ) from error


@timed("db.integrations.list")
def list_integrations(user_id: str) -> List[dict]:
    client = require_supabase()

    result = (
        client
        .table("user_ai_integrations")
        .select("id, provider, display_name, api_key_last4, created_at, updated_at")
        .eq("user_id", user_id)
        .order("created_at")
        .execute()
    )

    return result.data or []


@timed("db.integration.save")
def save_integration(user_id: str, ai_name: str, api_key: str) -> dict:
    clean_key = (api_key or "").strip()

    if len(clean_key) < 8:
        raise ValueError("Please enter a valid API key.")

    provider = normalize_provider_name(ai_name)
    display_name = PROVIDER_DISPLAY_NAMES[provider]
    last4 = clean_key[-4:]
    with mutation_guard("integration", (user_id, provider)), database_operation():
        client = require_supabase()
        payload = {
            "user_id": user_id,
            "provider": provider,
            "display_name": display_name,
            "encrypted_api_key": _encrypt_api_key(clean_key),
            "api_key_last4": last4,
        }
        # The existing migration defines UNIQUE(user_id, provider). Conflict
        # ownership is part of the key; another user's row cannot be replaced.
        result = (
            client.table("user_ai_integrations")
            .upsert(payload, on_conflict="user_id,provider")
            .select("id, user_id, provider, display_name, api_key_last4")
            .retry(False).execute()
        )
        rows = result.data
        if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
            raise PersistenceUnavailable()
        row = rows[0]
        if (
            not isinstance(row.get("id"), str) or not row["id"]
            or any(row.get(field) != payload[field] for field in (
                "user_id", "provider", "display_name", "api_key_last4",
            ))
        ):
            raise PersistenceUnavailable()
        return {
            "id": row["id"],
            "provider": provider,
            "display_name": display_name,
            "api_key_last4": last4,
        }


@timed("db.integration.delete")
def delete_integration(user_id: str, provider_name: str) -> None:
    provider = normalize_provider_name(provider_name)
    with mutation_guard("integration", (user_id, provider)), database_operation():
        client = require_supabase()
        result = (
            client.table("user_ai_integrations").delete(returning="minimal", count="exact")
            .eq("user_id", user_id).eq("provider", provider).retry(False).execute()
        )
        if affected_rows(result) > 1:
            raise PersistenceUnavailable()
        result = (
            client.table("user_ai_integrations").select("id", count="exact", head=True)
            .eq("user_id", user_id).eq("provider", provider).retry(False).execute()
        )
        affected_rows(result, expected=0)


def get_provider_api_key(user_id: str, provider_name: str) -> str | None:
    provider = normalize_provider_name(provider_name)
    client = require_supabase()

    result = (
        client
        .table("user_ai_integrations")
        .select("encrypted_api_key")
        .eq("user_id", user_id)
        .eq("provider", provider)
        .limit(1)
        .execute()
    )

    if not result.data:
        return None

    return _decrypt_api_key(result.data[0]["encrypted_api_key"])


@timed("db.integration.keys")
def get_user_provider_keys(user_id: str) -> Dict[str, str]:
    with database_operation():
        client = require_supabase()
        result = (
            client.table("user_ai_integrations").select("provider, encrypted_api_key")
            .eq("user_id", user_id).execute()
        )
        keys: Dict[str, str] = {}
        for row in result.data or []:
            provider = row.get("provider")
            encrypted_key = row.get("encrypted_api_key")
            if not provider or not encrypted_key:
                continue
            keys[provider] = _decrypt_api_key(encrypted_key)
        return keys
