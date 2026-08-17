from typing import List, Optional

from app.services.shared.search_signals import needs_google_search
from app.services.brain.context import HardRequirement, RoutingContext

FILE_KEYWORDS = [
    "this file", "uploaded", "attached", "document",
    ".pdf", ".csv", ".docx", ".xlsx", "spreadsheet"
]

VISION_KEYWORDS = [
    "this image", "this picture", "in the photo", "screenshot", "diagram shown"
]


def detect_search_requirement(context: RoutingContext) -> Optional[HardRequirement]:
    if needs_google_search(context.message):
        return HardRequirement(name="search", confirmed=False)
    return None


def detect_file_requirement(context: RoutingContext) -> Optional[HardRequirement]:
    if context.has_files:
        return HardRequirement(name="file", confirmed=True)

    lower = context.message.lower()
    if any(kw in lower for kw in FILE_KEYWORDS):
        return HardRequirement(name="file", confirmed=False)
    return None


def detect_vision_requirement(context: RoutingContext) -> Optional[HardRequirement]:
    if context.has_images:
        return HardRequirement(name="vision", confirmed=True)

    lower = context.message.lower()
    if any(kw in lower for kw in VISION_KEYWORDS):
        return HardRequirement(name="vision", confirmed=False)
    return None


def detect_hard_requirements(context: RoutingContext) -> List[HardRequirement]:
    detectors = [detect_search_requirement, detect_file_requirement, detect_vision_requirement]
    results = [d(context) for d in detectors]
    return [r for r in results if r is not None]