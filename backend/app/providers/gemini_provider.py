import os
from typing import Optional

from dotenv import load_dotenv
from google import genai
from google.genai import types

from app.services.conversation_service import load_recent_messages

load_dotenv()

DEFAULT_API_KEY = os.getenv("GEMINI_API_KEY")
MODEL = "gemini-2.5-flash"


def is_gemini_connected(api_key: Optional[str] = None) -> bool:
    return bool(api_key or DEFAULT_API_KEY)


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

    context_text = ""

    for msg in history:
        context_text += f"{msg['role'].upper()}: {msg['content']}\n\n"

    context_text += f"USER: {new_message}\n\n"
    return context_text


def call_gemini_search(
    conversation_id: str,
    message: str,
    api_key: Optional[str] = None,
    user_id: Optional[str] = None,
) -> str:
    key = api_key or DEFAULT_API_KEY

    if not key:
        raise Exception("Gemini API key is missing.")

    client = genai.Client(api_key=key)
    conversation_context = build_gemini_context(
        conversation_id=conversation_id,
        new_message=message,
        user_id=user_id,
    )

    philomath_prompt = f"""
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
{conversation_context}

Assistant answer:
"""

    grounding_tool = types.Tool(google_search=types.GoogleSearch())
    config = types.GenerateContentConfig(tools=[grounding_tool])

    response = client.models.generate_content(
        model=MODEL,
        contents=philomath_prompt,
        config=config,
    )

    return response.text or ""
