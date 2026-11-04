from typing import Literal, Optional

from pydantic import BaseModel, Field


# Uploads allow 500,000 extracted characters plus five bounded filenames and
# document separators in combined_text. Preserve that successful round trip.
MAX_CHAT_FILE_CONTEXT_CHARACTERS = 502_000


class ChatRequest(BaseModel):
    message: str = Field(max_length=32_000)
    mode: Literal["basic", "manual", "auto"] = "basic"
    conversation_id: Optional[str] = Field(default="default", max_length=200)
    provider: Optional[str] = Field(default=None, max_length=80)

    file_context: Optional[str] = Field(default=None, max_length=MAX_CHAT_FILE_CONTEXT_CHARACTERS)
    has_files: bool = False


class AddAIRequest(BaseModel):
    ai_name: str = Field(min_length=1, max_length=80)
    api_key: str = Field(min_length=8, max_length=500)
