import os
from typing import List

from dotenv import load_dotenv
from openai import OpenAI

from app.services.conversation_service import load_recent_messages

load_dotenv()

openrouter_api_key = os.getenv("OPENROUTER_API_KEY")

if openrouter_api_key:
    openrouter_client = OpenAI(
        base_url="https://openrouter.ai/api/v1",
        api_key=openrouter_api_key
    )
else:
    openrouter_client = None


def is_openrouter_connected() -> bool:
    return openrouter_client is not None


def build_openrouter_messages(conversation_id: str, new_message: str) -> List[dict]:
    history = load_recent_messages(conversation_id, limit=10)

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
            )
        }
    ]

    for msg in history:
        role = msg["role"]
        content = msg["content"]

        if role in ["user", "assistant"]:
            messages.append({
                "role": role,
                "content": content
            })

    messages.append({
        "role": "user",
        "content": new_message
    })

    return messages


def call_openrouter_basic(conversation_id: str, message: str) -> str:
    if openrouter_client is None:
        raise Exception("OpenRouter API key is missing. Add OPENROUTER_API_KEY to your .env file.")

    messages = build_openrouter_messages(conversation_id, message)

    response = openrouter_client.chat.completions.create(
        model="openrouter/free",
        messages=messages,
        max_tokens=700
    )

    return response.choices[0].message.content
