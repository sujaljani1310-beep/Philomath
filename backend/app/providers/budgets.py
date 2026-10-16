"""Explicit generation budgets shared by the five supported providers.

The input limit counts Unicode characters, including system prompts and recent
history. It is a resource bound, not a promise about a model's token capacity:
tokenization and context windows differ between provider models. Long input is
rejected rather than silently dropping history or changing provider selection.
"""

from collections.abc import Iterable

from fastapi import HTTPException


MAX_PROVIDER_INPUT_CHARS = 128_000
OPENAI_OUTPUT_TOKENS = 700  # Preserve all four existing compatible-provider caps.
GEMINI_OUTPUT_TOKENS = 8_192


class ProviderInputBudgetExceeded(HTTPException):
    def __init__(self) -> None:
        super().__init__(
            status_code=422,
            detail="Conversation input exceeds the supported size. Start a new conversation or shorten the message.",
        )


def validate_provider_input(parts: Iterable[str]) -> None:
    """Check sizes incrementally, before joining potentially large prompt parts."""
    total = 0
    for part in parts:
        total += len(part)
        if total > MAX_PROVIDER_INPUT_CHARS:
            raise ProviderInputBudgetExceeded()
