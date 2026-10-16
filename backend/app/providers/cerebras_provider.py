import os
from typing import List, Optional

from app.observability import timed_operation
from app.providers.budgets import OPENAI_OUTPUT_TOKENS, validate_provider_input
from app.providers.clients import openai_client, resolve_api_key
from app.services.conversation_service import load_recent_messages

DEFAULT_API_KEY = os.getenv("CEREBRAS_API_KEY")
BASE_URL = "https://api.cerebras.ai/v1"
MODEL = "llama3.1-8b"


def is_cerebras_connected(api_key: Optional[str] = None) -> bool:
    return bool(resolve_api_key(api_key, DEFAULT_API_KEY, "CEREBRAS_API_KEY"))


def build_cerebras_messages(
    conversation_id: str,
    new_message: str,
    user_id: Optional[str] = None,
) -> List[dict]:
    history = (
        load_recent_messages(conversation_id, user_id, limit=10)
        if user_id
        else []
    )

    messages = [
        {
            "role": "system",
            "content": (
                "You are Philomath using Cerebras. "
                "Answer clearly, helpfully, and directly. "
                "Use previous messages to understand follow-up questions. "
                "If the user asks for coding help, explain step by step. "
                "Do not make up facts."
            ),
        }
    ]

    for msg in history:
        if msg["role"] in ["user", "assistant"]:
            messages.append({
                "role": msg["role"],
                "content": msg["content"],
            })

    messages.append({"role": "user", "content": new_message})
    validate_provider_input(item["content"] for item in messages)
    return messages


def call_cerebras(
    conversation_id: str,
    message: str,
    api_key: Optional[str] = None,
    user_id: Optional[str] = None,
) -> str:
    key = resolve_api_key(api_key, DEFAULT_API_KEY, "CEREBRAS_API_KEY")

    if not key:
        raise Exception("Cerebras API key is missing.")

    messages = build_cerebras_messages(conversation_id, message, user_id)

    with timed_operation("provider.cerebras"), openai_client(BASE_URL, key) as client:
        response = client.chat.completions.create(
            model=MODEL,
            messages=messages,
            max_tokens=OPENAI_OUTPUT_TOKENS,
        )

        return response.choices[0].message.content or ""
