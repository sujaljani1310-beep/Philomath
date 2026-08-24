import os
from typing import Dict, List

from cryptography.fernet import Fernet, InvalidToken

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


def save_integration(user_id: str, ai_name: str, api_key: str) -> dict:
    clean_key = (api_key or "").strip()

    if len(clean_key) < 8:
        raise ValueError("Please enter a valid API key.")

    provider = normalize_provider_name(ai_name)
    display_name = PROVIDER_DISPLAY_NAMES[provider]
    encrypted_key = _encrypt_api_key(clean_key)
    last4 = clean_key[-4:]
    client = require_supabase()

    existing = (
        client
        .table("user_ai_integrations")
        .select("id")
        .eq("user_id", user_id)
        .eq("provider", provider)
        .limit(1)
        .execute()
    )

    payload = {
        "user_id": user_id,
        "provider": provider,
        "display_name": display_name,
        "encrypted_api_key": encrypted_key,
        "api_key_last4": last4,
    }

    if existing.data:
        integration_id = existing.data[0]["id"]
        result = (
            client
            .table("user_ai_integrations")
            .update(payload)
            .eq("id", integration_id)
            .eq("user_id", user_id)
            .execute()
        )
    else:
        result = (
            client
            .table("user_ai_integrations")
            .insert(payload)
            .execute()
        )

    row = (result.data or [payload])[0]

    return {
        "id": row.get("id"),
        "provider": provider,
        "display_name": display_name,
        "api_key_last4": last4,
    }


def delete_integration(user_id: str, provider_name: str) -> None:
    provider = normalize_provider_name(provider_name)
    client = require_supabase()

    (
        client
        .table("user_ai_integrations")
        .delete()
        .eq("user_id", user_id)
        .eq("provider", provider)
        .execute()
    )


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


def get_user_provider_keys(user_id: str) -> Dict[str, str]:
    client = require_supabase()

    result = (
        client
        .table("user_ai_integrations")
        .select("provider, encrypted_api_key")
        .eq("user_id", user_id)
        .execute()
    )

    keys: Dict[str, str] = {}

    for row in result.data or []:
        provider = row.get("provider")
        encrypted_key = row.get("encrypted_api_key")

        if not provider or not encrypted_key:
            continue

        keys[provider] = _decrypt_api_key(encrypted_key)

    return keys
