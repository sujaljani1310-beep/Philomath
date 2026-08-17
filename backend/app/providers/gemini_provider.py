import os

from dotenv import load_dotenv
from google import genai
from google.genai import types

from app.services.conversation_service import load_recent_messages

load_dotenv()

gemini_api_key = os.getenv("GEMINI_API_KEY")

if gemini_api_key:
    gemini_client = genai.Client(api_key=gemini_api_key)
else:
    gemini_client = None


def is_gemini_connected() -> bool:
    return gemini_client is not None


def build_gemini_context(conversation_id: str, new_message: str) -> str:
    history = load_recent_messages(conversation_id, limit=10)

    context_text = ""

    for msg in history:
        role = msg["role"]
        content = msg["content"]
        context_text += f"{role.upper()}: {content}\n\n"

    context_text += f"USER: {new_message}\n\n"

    return context_text


def call_gemini_search(conversation_id: str, message: str) -> str:
    if gemini_client is None:
        raise Exception("Gemini API key is missing. Add GEMINI_API_KEY to your .env file.")

    conversation_context = build_gemini_context(
        conversation_id=conversation_id,
        new_message=message
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

    grounding_tool = types.Tool(
        google_search=types.GoogleSearch()
    )

    config = types.GenerateContentConfig(
        tools=[grounding_tool]
    )

    response = gemini_client.models.generate_content(
        model="gemini-2.5-flash",
        contents=philomath_prompt,
        config=config
    )

    return response.text
