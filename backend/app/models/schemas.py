from typing import Optional

from pydantic import BaseModel


class ChatRequest(BaseModel):
    message: str
    mode: str = "basic"
    conversation_id: Optional[str] = "default"
    provider: Optional[str] = None

    file_context: Optional[str] = None
    has_files: bool = False