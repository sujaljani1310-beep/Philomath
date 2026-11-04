import os
from typing import Optional

from app.observability import timed_operation
from app.providers.budgets import GEMINI_OUTPUT_TOKENS, validate_provider_input
from app.providers.clients import (
    gemini_unused_async_client,
    provider_http_client,
    resolve_api_key,
)
from app.services.conversation_service import load_recent_messages
from app.settings import get_settings

DEFAULT_API_KEY = os.getenv("GEMINI_API_KEY")
MODEL = "gemini-2.5-flash"
BASE_URL = "https://generativelanguage.googleapis.com"

_PROMPT_PREFIX = """
You are Philomath Basic Mode.

Your job:
- Answer clearly.
- Keep answers simple and useful.
- Use the conversation history to understand follow-up questions.
- Use Google Search grounding for current/recent/specific topics.
- Do not confuse similarly named things.
- Do not make up facts.
- If you are unsure, say you are unsure.

Conversation:
"""
_PROMPT_SUFFIX = "\n\nAssistant answer:\n"


def is_gemini_connected(api_key: Optional[str] = None) -> bool:
    return bool(resolve_api_key(api_key, DEFAULT_API_KEY, "GEMINI_API_KEY"))


def build_gemini_context(
    conversation_id: str,
    new_message: str,
    user_id: Optional[str] = None,
) -> str:
    history = (
        load_recent_messages(conversation_id, user_id, limit=10)
        if user_id
        else []
    )

    # Keep references to content until the combined length is checked. Building
    # a joined prompt first would allocate unbounded strings from old history.
    parts = []
    for msg in history:
        validate_provider_input((msg["role"], msg["content"]))
        parts.extend((msg["role"].upper(), ": ", msg["content"], "\n\n"))

    parts.extend(("USER: ", new_message, "\n\n"))
    validate_provider_input(parts)
    return "".join(parts)


def call_gemini_search(
    conversation_id: str,
    message: str,
    api_key: Optional[str] = None,
    user_id: Optional[str] = None,
) -> str:
    key = resolve_api_key(api_key, DEFAULT_API_KEY, "GEMINI_API_KEY")

    if not key:
        raise Exception("Gemini API key is missing.")

    conversation_context = build_gemini_context(
        conversation_id=conversation_id,
        new_message=message,
        user_id=user_id,
    )

    validate_provider_input((_PROMPT_PREFIX, conversation_context, _PROMPT_SUFFIX))
    philomath_prompt = "".join((_PROMPT_PREFIX, conversation_context, _PROMPT_SUFFIX))

    from google import genai
    from google.genai import types

    grounding_tool = types.Tool(google_search=types.GoogleSearch())
    config = types.GenerateContentConfig(
        tools=[grounding_tool], max_output_tokens=GEMINI_OUTPUT_TOKENS,
    )

    settings = get_settings()
    with timed_operation("provider.gemini"), provider_http_client(BASE_URL) as http_client:
        with genai.Client(
            api_key=key,
            http_options=types.HttpOptions(
                httpx_client=http_client,
                httpx_async_client=gemini_unused_async_client(),
                timeout=max(1, int(settings.provider_timeout_seconds * 1000)),
                retry_options=types.HttpRetryOptions(
                    # Generation POSTs can succeed before a timeout or lost
                    # response. Do not replay an ambiguous non-idempotent call.
                    attempts=1,
                    initial_delay=0.5,
                    max_delay=2,
                ),
            ),
        ) as client:
            response = client.models.generate_content(
                model=MODEL,
                contents=philomath_prompt,
                config=config,
            )

            return response.text or ""
