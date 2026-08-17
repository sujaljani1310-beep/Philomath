import os
from typing import List

from dotenv import load_dotenv
from openai import OpenAI

from app.services.conversation_service import load_recent_messages

load_dotenv()

grok_api_key = os.getenv("GROK_API_KEY")

if grok_api_key:
    grok_client = OpenAI(
        base_url="https://api.x.ai/v1",
        api_key=grok_api_key
    )
else:
    grok_client = None


def is_grok_connected() -> bool:
    return grok_client is not None


def build_grok_messages(conversation_id: str, new_message: str) -> List[dict]:
    history = load_recent_messages(conversation_id, limit=10)

    messages = [
        {
            "role": "system",
            "content": (
                "You are Philomath Manual Mode using Grok. "
                "Answer clearly, helpfully, and directly. "
                "Use previous messages to understand follow-up questions. "
                "If the user asks for coding help, explain step by step. "
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


def call_grok(conversation_id: str, message: str) -> str:
    if grok_client is None:
        raise Exception("Grok API key is missing. Add GROK_API_KEY to your .env file.")

    messages = build_grok_messages(conversation_id, message)

    response = grok_client.chat.completions.create(
        model="grok-4.3",
        messages=messages,
        max_tokens=700
    )

    return response.choices[0].message.content