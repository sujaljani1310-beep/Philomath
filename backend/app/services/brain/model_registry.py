from dataclasses import dataclass, field
from typing import Dict, Iterable, Optional

CATEGORIES = [
    "coding", "math", "reasoning", "creative",
    "research", "summarization", "long_context",
    "file_related", "general",
]


@dataclass
class ModelCapability:
    key: str
    model_used: str
    capabilities: Dict[str, float] = field(default_factory=dict)
    hard_flags: Dict[str, bool] = field(default_factory=dict)
    speed: float = 0.5
    cost: float = 0.5
    reliability: float = 0.8
    enabled: bool = True

    def supports(self, requirement_name: str) -> bool:
        return self.hard_flags.get(requirement_name, False)


MODEL_REGISTRY: Dict[str, ModelCapability] = {
    "cerebras": ModelCapability(
        key="cerebras",
        model_used="llama3.1-8b",
        capabilities={
            "coding": 0.55, "math": 0.45, "reasoning": 0.5, "creative": 0.5,
            "research": 0.2, "summarization": 0.5, "long_context": 0.35,
            "file_related": 0.3, "general": 0.7,
        },
        hard_flags={"search": False, "file": False, "vision": False},
        speed=0.98, cost=0.05, reliability=0.9,
    ),
    "nvidia": ModelCapability(
        key="nvidia",
        model_used="meta/llama-3.1-8b-instruct",
        capabilities={
            "coding": 0.55, "math": 0.45, "reasoning": 0.5, "creative": 0.5,
            "research": 0.2, "summarization": 0.5, "long_context": 0.35,
            "file_related": 0.3, "general": 0.7,
        },
        hard_flags={"search": False, "file": False, "vision": False},
        speed=0.75, cost=0.1, reliability=0.85,
    ),
    "openrouter": ModelCapability(
        key="openrouter",
        model_used="openrouter/free",
        capabilities={cat: 0.35 for cat in CATEGORIES},
        hard_flags={"search": False, "file": False, "vision": False},
        speed=0.5, cost=0.0, reliability=0.55,
    ),
    "gemini": ModelCapability(
        key="gemini",
        model_used="gemini-2.5-flash",
        capabilities={
            "coding": 0.55, "math": 0.5, "reasoning": 0.6, "creative": 0.55,
            "research": 0.95, "summarization": 0.75, "long_context": 0.7,
            "file_related": 0.5, "general": 0.65,
        },
        hard_flags={"search": True, "file": False, "vision": False},
        speed=0.7, cost=0.3, reliability=0.85,
    ),
    "grok": ModelCapability(
        key="grok",
        model_used="grok-4.3",
        capabilities={
            "coding": 0.9, "math": 0.85, "reasoning": 0.9, "creative": 0.8,
            "research": 0.5, "summarization": 0.7, "long_context": 0.7,
            "file_related": 0.5, "general": 0.8,
        },
        hard_flags={"search": False, "file": False, "vision": False},
        speed=0.6, cost=0.8, reliability=0.9,
    ),
}


def get_available_models(
    available_provider_keys: Optional[Iterable[str]] = None,
) -> Dict[str, ModelCapability]:
    """
    Return models the current signed-in user actually configured.

    When available_provider_keys is None, return all enabled registry models.
    The explicit user-provider list is used by Auto Mode so one user's keys can
    never make a provider appear available to another user.
    """
    allowed = (
        set(available_provider_keys)
        if available_provider_keys is not None
        else None
    )

    return {
        key: model
        for key, model in MODEL_REGISTRY.items()
        if model.enabled and (allowed is None or key in allowed)
    }
