"""Validated process settings; optional SDKs and credentials stay out of imports."""

import math
import os
from dataclasses import dataclass
from functools import lru_cache


@dataclass(frozen=True)
class Settings:
    connect_timeout_seconds: float = 5.0
    auth_timeout_seconds: float = 10.0
    database_timeout_seconds: float = 15.0
    provider_timeout_seconds: float = 30.0
    provider_connect_timeout_seconds: float = 5.0
    # Legacy setting is still parsed for configuration compatibility. Generation
    # POSTs always use one attempt because providers share no idempotency key.
    provider_max_retries: int = 0
    performance_logging_enabled: bool = True


def _positive_seconds(name: str, default: float) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except ValueError:
        raise ValueError(f"{name} must be a positive finite number") from None
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be a positive finite number")
    return value


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    # Preserve supported backend/.env configuration without repeated scans in
    # every provider. No credentials are stored in the settings object or logs.
    from dotenv import load_dotenv

    load_dotenv()
    retries_name = "PHILOMATH_PROVIDER_MAX_RETRIES"
    try:
        retries = int(os.getenv(retries_name, "0"))
    except ValueError:
        raise ValueError(f"{retries_name} must be an integer between 0 and 2") from None
    if not 0 <= retries <= 2:
        raise ValueError(f"{retries_name} must be an integer between 0 and 2")
    logging_name = "PHILOMATH_PERFORMANCE_LOGGING"
    logging_value = os.getenv(logging_name, "true").lower()
    if logging_value not in {"true", "false", "1", "0"}:
        raise ValueError(f"{logging_name} must be true, false, 1, or 0")
    return Settings(
        connect_timeout_seconds=_positive_seconds("PHILOMATH_CONNECT_TIMEOUT_SECONDS", 5),
        auth_timeout_seconds=_positive_seconds("PHILOMATH_AUTH_TIMEOUT_SECONDS", 10),
        database_timeout_seconds=_positive_seconds("PHILOMATH_DATABASE_TIMEOUT_SECONDS", 15),
        provider_timeout_seconds=_positive_seconds("PHILOMATH_PROVIDER_TIMEOUT_SECONDS", 30),
        provider_connect_timeout_seconds=_positive_seconds("PHILOMATH_PROVIDER_CONNECT_TIMEOUT_SECONDS", 5),
        provider_max_retries=retries,
        performance_logging_enabled=logging_value in {"true", "1"},
    )
