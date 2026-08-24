import os
from typing import List, Optional

from dotenv import load_dotenv
from openai import OpenAI

from app.services.conversation_service import load_recent_messages

load_dotenv()

DEFAULT_API_KEY = os.getenv("CEREBRAS_API_KEY")
BASE_URL = "https://api.cerebras.ai/v1"
MODEL = "llama3.1-8b"


def is_cerebras_connected(api_key: Optional[str] = None) -> bool:
    return bool(api_key or DEFAULT_API_KEY)


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
    return messages


def call_cerebras(
    conversation_id: str,
    message: str,
    api_key: Optional[str] = None,
    user_id: Optional[str] = None,
) -> str:
    key = api_key or DEFAULT_API_KEY

    if not key:
        raise Exception("Cerebras API key is missing.")

    client = OpenAI(base_url=BASE_URL, api_key=key)
    messages = build_cerebras_messages(conversation_id, message, user_id)

    response = client.chat.completions.create(
        model=MODEL,
        messages=messages,
        max_tokens=700,
    )

    return response.choices[0].message.content or ""
