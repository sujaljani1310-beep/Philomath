from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from app.services.brain.context import HardRequirement
from app.services.brain.model_registry import ModelCapability
from app.services.brain.profiler import RequestProfile


CAPABILITY_WEIGHT = 0.55
SPEED_WEIGHT = 0.15
COST_WEIGHT = 0.10
RELIABILITY_WEIGHT = 0.20

# Used internally to decide whether Philomath should ask the cheap
# classifier fallback for help disambiguating a request.
MARGIN_CONFIDENCE_THRESHOLD = 0.12

# Used only when converting the raw winner-vs-runner-up margin into
# a user-friendly confidence value.
MARGIN_SCALE = 0.30


@dataclass
class RankedModel:
    key: str
    model_used: str
    score: float


@dataclass
class RoutingConfidence:
    level: str
    value: float


@dataclass
class RoutingDecision:
    ranked: List[RankedModel]
    confidence: RoutingConfidence
    dominant_category: str
    reasoning: str

    used_classifier_fallback: bool = False
    hard_requirements_unmet: bool = False

    blocked: bool = False
    blocked_reason: Optional[str] = None

    # Internal routing signal.
    # Never expose this directly as a "confidence percentage".
    _raw_margin: float = 0.0


def filter_by_hard_requirements(
    available: Dict[str, ModelCapability],
    requirements: List[HardRequirement],
) -> Tuple[
    Dict[str, ModelCapability],
    List[HardRequirement],
    List[HardRequirement],
]:
    """
    Remove models that cannot satisfy required capabilities.

    Returns:
        filtered_models
        unmet_confirmed_requirements
        unmet_inferred_requirements
    """

    if not requirements:
        return available, [], []

    filtered = {
        key: model
        for key, model in available.items()
        if all(
            model.supports(requirement.name)
            for requirement in requirements
        )
    }

    if filtered:
        return filtered, [], []

    unmet_confirmed = [
        requirement
        for requirement in requirements
        if requirement.confirmed
    ]

    unmet_inferred = [
        requirement
        for requirement in requirements
        if not requirement.confirmed
    ]

    return {}, unmet_confirmed, unmet_inferred


def _capability_match(
    model: ModelCapability,
    profile: RequestProfile,
) -> float:
    """
    Measure how well a model's strengths match the request profile.
    """

    total_weight = sum(profile.scores.values())

    if total_weight <= 0:
        return 0.0

    matched_score = sum(
        profile.scores.get(category, 0.0)
        * model.capabilities.get(category, 0.0)
        for category in profile.scores
    )

    return matched_score / total_weight


def _score_model(
    model: ModelCapability,
    profile: RequestProfile,
) -> float:
    """
    Produce the final ranking score for a model.

    Higher score = better routing candidate.
    """

    capability_score = _capability_match(model, profile)

    score = (
        CAPABILITY_WEIGHT * capability_score
        + SPEED_WEIGHT * model.speed
        + COST_WEIGHT * (1.0 - model.cost)
        + RELIABILITY_WEIGHT * model.reliability
    )

    return max(0.0, min(score, 1.0))


def _confidence_from(
    top_score: float,
    margin: float,
) -> RoutingConfidence:
    """
    Convert routing quality into a user-friendly confidence value.

    Confidence combines:

    1. Absolute quality of the winning model.
    2. How clearly it beat the runner-up.

    The raw margin itself is NOT presented as a confidence percentage.
    """

    if margin > 0:
        normalized_margin = min(
            margin / MARGIN_SCALE,
            1.0,
        )
    else:
        normalized_margin = 0.0

    combined = (
        0.70 * top_score
        + 0.30 * normalized_margin
    )

    combined = max(
        0.0,
        min(combined, 1.0),
    )

    if combined >= 0.75:
        level = "High"
    elif combined >= 0.50:
        level = "Medium"
    else:
        level = "Low"

    return RoutingConfidence(
        level=level,
        value=round(combined, 2),
    )


def rank_models(
    profile: RequestProfile,
    available: Dict[str, ModelCapability],
) -> RoutingDecision:
    """
    Score and rank all currently eligible models.
    """

    scored = [
        RankedModel(
            key=key,
            model_used=model.model_used,
            score=_score_model(model, profile),
        )
        for key, model in available.items()
    ]

    scored.sort(
        key=lambda model: model.score,
        reverse=True,
    )

    if len(scored) >= 2:
        margin = round(
            scored[0].score - scored[1].score,
            3,
        )

        confidence = _confidence_from(
            scored[0].score,
            margin,
        )

    elif len(scored) == 1:
        # There is no runner-up, so there is no meaningful
        # selection margin.
        margin = 0.0

        confidence = _confidence_from(
            scored[0].score,
            margin,
        )

    else:
        margin = 0.0

        confidence = RoutingConfidence(
            level="Low",
            value=0.0,
        )

    if scored:
        reasoning = (
            f"Detected primary intent: "
            f"{profile.dominant_category}. "
            f"Top pick: {scored[0].key} "
            f"(score={scored[0].score:.2f})."
        )
    else:
        reasoning = "No available models to route to."

    decision = RoutingDecision(
        ranked=scored,
        confidence=confidence,
        dominant_category=profile.dominant_category,
        reasoning=reasoning,
    )

    decision._raw_margin = margin

    return decision