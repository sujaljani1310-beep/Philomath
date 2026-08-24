from typing import Optional

from pydantic import BaseModel, Field


class ChatRequest(BaseModel):
    message: str
    mode: str = "basic"
    conversation_id: Optional[str] = "default"
    provider: Optional[str] = None

    file_context: Optional[str] = None
    has_files: bool = False


class AddAIRequest(BaseModel):
    ai_name: str = Field(min_length=1, max_length=80)
    api_key: str = Field(min_length=8, max_length=500)
