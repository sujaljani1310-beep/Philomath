import re
from dataclasses import dataclass, field
from typing import Dict, List, Protocol

from app.services.brain.context import HardRequirement, RoutingContext
from app.services.brain.signals import detect_hard_requirements

CATEGORIES = [
    "coding",
    "math",
    "reasoning",
    "creative",
    "research",
    "summarization",
    "long_context",
    "file_related",
    "general",
]


class SignalSource(Protocol):
    """
    Anything that can score a message across CATEGORIES.

    KeywordSignals and StructuralSignals are cheap and run on every
    request. A future local semantic or embedding classifier can implement
    this same interface and be added without changing the scorer/router.
    """

    def score(self, message: str) -> Dict[str, float]:
        ...


class KeywordSignals:
    """Cheap deterministic keyword and pattern matching."""

    def score(self, message: str) -> Dict[str, float]:
        lower = message.lower()

        return {
            "coding": self._coding(message, lower),
            "math": self._math(lower),
            "reasoning": self._reasoning(lower),
            "creative": self._creative(lower),
            "research": self._research(lower),
            "summarization": self._summarization(lower),
            "long_context": 0.0,
            "file_related": self._file(lower),
            "general": 0.3,
        }

    def _coding(self, message: str, lower: str) -> float:
        score = 0.0

        if "```" in message or re.search(
            r"\b(def|class|import|function|return|const|let|var)\b",
            lower,
        ):
            score += 0.5

        if any(
            phrase in lower
            for phrase in [
                "error",
                "traceback",
                "bug",
                "fix this code",
                "stack trace",
                "exception",
                "compile",
                "debug",
            ]
        ):
            score += 0.3

        if any(
            word in lower
            for word in [
                "python",
                "javascript",
                "typescript",
                "java",
                "c++",
                "sql",
                "api",
                "algorithm",
                "refactor",
                "function",
                "program",
            ]
        ):
            score += 0.2

        return min(score, 1.0)

    def _math(self, lower: str) -> float:
        score = 0.0

        if re.search(r"[0-9].*[\+\-\*/=^].*[0-9]", lower) or "√" in lower:
            score += 0.4

        if any(
            word in lower
            for word in [
                "solve",
                "equation",
                "integral",
                "derivative",
                "calculate",
                "proof",
                "theorem",
                "probability",
                "matrix",
                "algebra",
                "geometry",
                "statistics",
            ]
        ):
            score += 0.5

        return min(score, 1.0)

    def _reasoning(self, lower: str) -> float:
        score = 0.0

        if any(
            phrase in lower
            for phrase in [
                "why",
                "step by step",
                "reasoning",
                "analyze",
                "pros and cons",
                "compare",
                "trade-off",
                "tradeoff",
                "explain why",
                "what if",
            ]
        ):
            score += 0.5

        return min(score, 1.0)

    def _creative(self, lower: str) -> float:
        score = 0.0

        if any(
            phrase in lower
            for phrase in [
                "write a story",
                "poem",
                "creative",
                "imagine",
                "fictional",
                "brainstorm",
                "screenplay",
                "lyrics",
                "character",
                "write a scene",
            ]
        ):
            score += 0.6

        return min(score, 1.0)

    def _research(self, lower: str) -> float:
        score = 0.0

        if any(
            phrase in lower
            for phrase in [
                "research",
                "find information",
                "sources",
                "cite",
                "according to",
                "look up",
                "find sources",
            ]
        ):
            score += 0.3

        return min(score, 1.0)

    def _summarization(self, lower: str) -> float:
        score = 0.0

        if any(
            phrase in lower
            for phrase in [
                "summarize",
                "tl;dr",
                "tldr",
                "shorten",
                "condense",
                "key points",
                "main points",
            ]
        ):
            score += 0.6

        return min(score, 1.0)

    def _file(self, lower: str) -> float:
        score = 0.0

        if any(
            phrase in lower
            for phrase in [
                "this file",
                "uploaded",
                "attached",
                "document",
                ".pdf",
                ".csv",
                ".docx",
                ".xlsx",
                "spreadsheet",
            ]
        ):
            score += 0.7

        return min(score, 1.0)


class StructuralSignals:
    """Deterministic analysis of the structure and shape of the message."""

    def score(self, message: str) -> Dict[str, float]:
        word_count = len(message.split())
        lines = message.splitlines()

        code_lines = sum(
            1
            for line in lines
            if re.match(
                r"^\s*(def |class |import |from |return |if |for |while |[{}();]|#|//)",
                line,
            )
        )

        code_ratio = code_lines / len(lines) if lines else 0.0

        digit_ratio = (
            sum(character.isdigit() for character in message)
            / max(len(message), 1)
        )

        question_words = len(
            re.findall(
                r"\b(why|how|what if|explain|compare|analyze)\b",
                message.lower(),
            )
        )

        return {
            "coding": min(code_ratio * 1.5, 1.0),
            "math": min(digit_ratio * 3.0, 0.6),
            "reasoning": min(question_words * 0.25, 0.6),
            "creative": 0.0,
            "research": 0.0,
            "summarization": 0.2 if word_count > 300 else 0.0,
            "long_context": self._long_context(word_count),
            "file_related": 0.0,
            "general": 0.0,
        }

    def _long_context(self, word_count: int) -> float:
        if word_count > 1200:
            return 1.0

        if word_count > 600:
            return 0.9

        if word_count > 300:
            return 0.5

        return 0.0


DEFAULT_SIGNAL_SOURCES: List[SignalSource] = [
    KeywordSignals(),
    StructuralSignals(),
]


@dataclass
class RequestProfile:
    scores: Dict[str, float] = field(default_factory=dict)
    dominant_category: str = "general"
    hard_requirements: List[HardRequirement] = field(default_factory=list)
    word_count: int = 0


def _merge_scores(
    outputs: List[Dict[str, float]],
) -> Dict[str, float]:
    """
    Combine signal sources by averaging them instead of simply adding
    and clipping.

    This keeps the system stable as more signal sources are added later.
    """

    if not outputs:
        return {category: 0.0 for category in CATEGORIES}

    merged = {category: 0.0 for category in CATEGORIES}

    for output in outputs:
        for category in CATEGORIES:
            merged[category] += output.get(category, 0.0)

    number_of_sources = len(outputs)

    for category in merged:
        merged[category] = merged[category] / number_of_sources

    return merged


def profile_request(
    context: RoutingContext,
    sources: List[SignalSource] = None,
) -> RequestProfile:
    active_sources = sources or DEFAULT_SIGNAL_SOURCES

    outputs = [
        source.score(context.message)
        for source in active_sources
    ]

    scores = _merge_scores(outputs)

    dominant_category = max(
        scores,
        key=scores.get,
    )

    return RequestProfile(
        scores=scores,
        dominant_category=dominant_category,
        hard_requirements=detect_hard_requirements(context),
        word_count=len(context.message.split()),
    )