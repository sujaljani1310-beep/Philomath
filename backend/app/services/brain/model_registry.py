from dataclasses import dataclass, field
from typing import Dict

from app.providers.cerebras_provider import is_cerebras_connected
from app.providers.nvidia_provider import is_nvidia_connected
from app.providers.openrouter_provider import is_openrouter_connected
from app.providers.gemini_provider import is_gemini_connected
from app.providers.grok_provider import is_grok_connected

CATEGORIES = [
    "coding", "math", "reasoning", "creative",
    "research", "summarization", "long_context",
    "file_related", "general"
]


@dataclass
class ModelCapability:
    key: str
    model_used: str
    capabilities: Dict[str, float] = field(default_factory=dict)
    hard_flags: Dict[str, bool] = field(default_factory=dict)  # e.g. {"search": True, "file": False, "vision": False}
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
        speed=0.98, cost=0.05, reliability=0.9, enabled=True,
    ),
    "nvidia": ModelCapability(
        key="nvidia",
        model_used="meta/llama-3.1-8b-instruct",
        capabilities={
            # same base weights as Cerebras -> same capability profile,
            # differ only on speed/cost/reliability
            "coding": 0.55, "math": 0.45, "reasoning": 0.5, "creative": 0.5,
            "research": 0.2, "summarization": 0.5, "long_context": 0.35,
            "file_related": 0.3, "general": 0.7,
        },
        hard_flags={"search": False, "file": False, "vision": False},
        speed=0.75, cost=0.1, reliability=0.85, enabled=True,
    ),
    "openrouter": ModelCapability(
        key="openrouter",
        model_used="openrouter/free",
        capabilities={cat: 0.35 for cat in CATEGORIES},
        hard_flags={"search": False, "file": False, "vision": False},
        speed=0.5, cost=0.0, reliability=0.55, enabled=True,
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
        speed=0.7, cost=0.3, reliability=0.85, enabled=True,
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
        enabled=False,  # no credits -- flip to True to re-enable, nothing else to change
    ),
}

_CONNECTIVITY_CHECKS = {
    "cerebras": is_cerebras_connected,
    "nvidia": is_nvidia_connected,
    "openrouter": is_openrouter_connected,
    "gemini": is_gemini_connected,
    "grok": is_grok_connected,
}


def get_available_models() -> Dict[str, ModelCapability]:
    available = {}
    for key, model in MODEL_REGISTRY.items():
        if not model.enabled:
            continue
        check_fn = _CONNECTIVITY_CHECKS.get(key)
        if check_fn and check_fn():
            available[key] = model
    return available