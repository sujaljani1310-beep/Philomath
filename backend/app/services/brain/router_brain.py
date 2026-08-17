from app.services.brain.classifier_fallback import classify_ambiguous_request
from app.services.brain.context import RoutingContext
from app.services.brain.model_registry import get_available_models
from app.services.brain.profiler import profile_request
from app.services.brain.scorer import (
    MARGIN_CONFIDENCE_THRESHOLD,
    RoutingConfidence,
    RoutingDecision,
    filter_by_hard_requirements,
    rank_models,
)


def route(context: RoutingContext) -> RoutingDecision:
    """
    Main Philomath Auto Mode routing pipeline.

    Flow:
        1. Profile the request
        2. Find currently available models
        3. Apply hard capability requirements
        4. Rank compatible models
        5. Use the lightweight classifier only when routing is ambiguous
        6. Return the ranked fallback chain
    """

    profile = profile_request(context)
    available_models = get_available_models()

    filtered_models, unmet_confirmed, unmet_inferred = (
        filter_by_hard_requirements(
            available_models,
            profile.hard_requirements,
        )
    )

    # A confirmed requirement comes from real context such as an actual
    # uploaded file/image. If Philomath cannot satisfy it, do not pretend
    # that the request can be processed correctly.
    if unmet_confirmed:
        requirement_names = ", ".join(
            requirement.name
            for requirement in unmet_confirmed
        )

        return RoutingDecision(
            ranked=[],
            confidence=RoutingConfidence(
                level="Low",
                value=0.0,
            ),
            dominant_category=profile.dominant_category,
            reasoning=(
                "Philomath does not currently have a connected model "
                f"that supports the required capability: {requirement_names}."
            ),
            blocked=True,
            blocked_reason=(
                f"This request requires {requirement_names} support, "
                "which Philomath cannot currently provide."
            ),
        )

    # If the requirement was only inferred from wording, rather than
    # confirmed by actual attachment/context metadata, allow Philomath
    # to continue with its normal model pool as a best-effort fallback.
    if filtered_models:
        working_models = filtered_models
    else:
        working_models = available_models

    decision = rank_models(
        profile,
        working_models,
    )

    if unmet_inferred and not filtered_models:
        decision.hard_requirements_unmet = True

        requirement_names = ", ".join(
            requirement.name
            for requirement in unmet_inferred
        )

        decision.reasoning += (
            f" Inferred requirement '{requirement_names}' could not be "
            "confirmed, so routing continued in best-effort mode."
        )

    # Only ask the lightweight Cerebras classifier for help when:
    # - multiple models are eligible
    # - Philomath's own deterministic router cannot clearly separate them
    #
    # Cerebras does NOT become the primary router.
    if (
        decision.ranked
        and len(working_models) > 1
        and decision._raw_margin < MARGIN_CONFIDENCE_THRESHOLD
    ):
        classified_category = classify_ambiguous_request(
            context.message
        )

        if classified_category:
            updated_scores = dict(profile.scores)

            # Strengthen the category selected by the fallback classifier.
            updated_scores[classified_category] = max(
                updated_scores.get(classified_category, 0.0),
                1.0,
            )

            profile.scores = updated_scores
            profile.dominant_category = classified_category

            decision = rank_models(
                profile,
                working_models,
            )

            decision.used_classifier_fallback = True
            decision.hard_requirements_unmet = bool(
                unmet_inferred
                and not filtered_models
            )

            decision.reasoning += (
                " Low routing separation triggered the lightweight "
                "classifier fallback."
            )

    return decision