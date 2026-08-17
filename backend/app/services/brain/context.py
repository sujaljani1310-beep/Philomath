from dataclasses import dataclass, field
from typing import List


@dataclass
class RoutingContext:
    """
    Structured input to the router. `message` is required; the rest is
    real attachment/context metadata, not text inference.

    File Upload (and any future tool) should populate has_files /
    file_types / has_images directly from actual attachments -- never
    by re-parsing the message text. That's what lets the router tell
    "a PDF is actually attached" apart from "the user typed the word PDF."
    """
    message: str
    has_files: bool = False
    file_types: List[str] = field(default_factory=list)
    has_images: bool = False


@dataclass
class HardRequirement:
    name: str          # "search" / "file" / "vision" / future tool names
    confirmed: bool     # True = backed by real attachment/context metadata
                         # False = inferred from message text only