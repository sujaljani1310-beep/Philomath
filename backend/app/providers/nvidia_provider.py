import os
from typing import List

from dotenv import load_dotenv
from openai import OpenAI

from app.services.conversation_service import load_recent_messages

load_dotenv()

nvidia_api_key = os.getenv("NVIDIA_API_KEY")

if nvidia_api_key:
    nvidia_client = OpenAI(
        base_url="https://integrate.api.nvidia.com/v1",
        api_key=nvidia_api_key
    )
else:
    nvidia_client = None


def is_nvidia_connected() -> bool:
    return nvidia_client is not None


def build_nvidia_messages(conversation_id: str, new_message: str) -> List[dict]:
    history = load_recent_messages(conversation_id, limit=10)

    messages = [
        {
            "role": "system",
            "content": (
                "You are Philomath Manual Mode using NVIDIA NIM. "
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


def call_nvidia(conversation_id: str, message: str) -> str:
    if nvidia_client is None:
        raise Exception("NVIDIA API key is missing. Add NVIDIA_API_KEY to your .env file.")

    messages = build_nvidia_messages(conversation_id, message)

    response = nvidia_client.chat.completions.create(
        model="meta/llama-3.1-8b-instruct",
        messages=messages,
        max_tokens=700
    )

    return response.choices[0].message.content