import os
from typing import List, Optional

from dotenv import load_dotenv
from openai import OpenAI

from app.services.conversation_service import load_recent_messages

load_dotenv()

DEFAULT_API_KEY = os.getenv("OPENROUTER_API_KEY")
BASE_URL = "https://openrouter.ai/api/v1"
MODEL = "openrouter/free"


def is_openrouter_connected(api_key: Optional[str] = None) -> bool:
    return bool(api_key or DEFAULT_API_KEY)


def build_openrouter_messages(
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
                "You are Philomath Basic Mode. "
                "Answer clearly, simply, and helpfully. "
                "Use previous messages to understand follow-up questions. "
                "For beginner learning questions, explain step by step. "
                "If you are unsure, say you are unsure. "
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
    return messages


def call_openrouter_basic(
    conversation_id: str,
    message: str,
    api_key: Optional[str] = None,
    user_id: Optional[str] = None,
) -> str:
    key = api_key or DEFAULT_API_KEY

    if not key:
        raise Exception("OpenRouter API key is missing.")

    client = OpenAI(base_url=BASE_URL, api_key=key)
    messages = build_openrouter_messages(conversation_id, message, user_id)

    response = client.chat.completions.create(
        model=MODEL,
        messages=messages,
        max_tokens=700,
    )

    return response.choices[0].message.content or ""
