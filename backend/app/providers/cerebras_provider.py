import os
from typing import List

from dotenv import load_dotenv
from openai import OpenAI

from app.services.conversation_service import load_recent_messages

load_dotenv()

cerebras_api_key = os.getenv("CEREBRAS_API_KEY")

if cerebras_api_key:
    cerebras_client = OpenAI(
        base_url="https://api.cerebras.ai/v1",
        api_key=cerebras_api_key
    )
else:
    cerebras_client = None


def is_cerebras_connected() -> bool:
    return cerebras_client is not None


def build_cerebras_messages(conversation_id: str, new_message: str) -> List[dict]:
    history = load_recent_messages(conversation_id, limit=10)

    messages = [
        {
            "role": "system",
            "content": (
                "You are Philomath Manual Mode using Cerebras. "
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


def call_cerebras(conversation_id: str, message: str) -> str:
    if cerebras_client is None:
        raise Exception("Cerebras API key is missing. Add CEREBRAS_API_KEY to your .env file.")

    messages = build_cerebras_messages(conversation_id, message)

    response = cerebras_client.chat.completions.create(
        model="llama3.1-8b",
        messages=messages,
        max_tokens=700
    )

    return response.choices[0].message.content