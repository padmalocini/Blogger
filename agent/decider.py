from __future__ import annotations

from dataclasses import dataclass

try:
    from .enums import AlignmentCase, SearchIntent
    from .models import ClassificationResult, IntentLabel, UserInput
except ImportError:
    from enums import AlignmentCase, SearchIntent
    from models import ClassificationResult, IntentLabel, UserInput


# A label that is both ambiguous AND has one of these confidence levels is
# treated as "unclear" by the decider, even if the LLM picked an intent.
# Use ("low",) for a more lenient check, or ("medium", "low") for a stricter one.
VAGUE_CONFIDENCE_LEVELS: tuple[str, ...] = ("medium", "low")


def is_vague(label: IntentLabel) -> bool:
    """Return True if a label is 'unclear', or ambiguous with weak confidence."""
    if label.intent == SearchIntent.unclear:
        return True
    return label.ambiguous and label.confidence in VAGUE_CONFIDENCE_LEVELS


@dataclass
class DeciderOutcome:
    """Internal outcome container for AlignmentDecider."""
    case: AlignmentCase
    topic_intent: SearchIntent
    keyword_intent: SearchIntent
    resolved_intent: SearchIntent
    resolved_by: str


class AlignmentDecider:
    """Pure Python decision logic determining alignment cases from classification."""

    def decide(
        self,
        user_input: UserInput,
        classification: ClassificationResult,
    ) -> DeciderOutcome:

        topic_label = classification.topic
        topic_intent = topic_label.intent

        # Compute keyword_intent: majority (>= 60%) among keywords, else 'mixed'
        total_keywords = len(classification.keywords)
        intent_counts: dict[SearchIntent, int] = {}
        for kw in classification.keywords:
            intent_counts[kw.intent] = intent_counts.get(kw.intent, 0) + 1

        keyword_intent = SearchIntent.mixed
        for intent_candidate, count in intent_counts.items():
            if (count / total_keywords) >= 0.60:
                keyword_intent = intent_candidate
                break

        # 0. user_override is True -> case (b), proceed.
        if user_input.user_override:
            resolved = user_input.intent or topic_intent
            return DeciderOutcome(
                case=AlignmentCase.b,
                topic_intent=topic_intent,
                keyword_intent=keyword_intent,
                resolved_intent=resolved,
                resolved_by="user_override",
            )

        # Vagueness checks (unclear intent OR ambiguous + weak confidence)
        topic_is_unclear = is_vague(topic_label)
        keywords_are_unclear = (
            keyword_intent == SearchIntent.unclear
            or all(is_vague(k) for k in classification.keywords)
        )

        # 1. topic AND keywords both unclear -> case (d).
        if topic_is_unclear and keywords_are_unclear:
            return DeciderOutcome(
                case=AlignmentCase.d,
                topic_intent=topic_intent,
                keyword_intent=keyword_intent,
                resolved_intent=SearchIntent.unclear,
                resolved_by="unresolved",
            )

        # 2. No clear intent for topic or keywords -> case (a).
        if topic_is_unclear or keywords_are_unclear:
            return DeciderOutcome(
                case=AlignmentCase.a,
                topic_intent=topic_intent,
                keyword_intent=keyword_intent,
                resolved_intent=topic_intent,
                resolved_by="unresolved",
            )

        # 3. keyword intent incompatible with topic intent -> case (c).
        # 'mixed' is compatible with anything; an intent is also compatible if
        # it equals the topic's secondary_intent.
        is_compatible = False
        if keyword_intent == SearchIntent.mixed or topic_intent == SearchIntent.mixed:
            is_compatible = True
        elif keyword_intent == topic_intent:
            is_compatible = True
        elif (
            topic_label.secondary_intent is not None
            and keyword_intent == topic_label.secondary_intent
        ):
            is_compatible = True

        if not is_compatible:
            # If the user already chose which intent takes precedence (by setting
            # UserInput.intent to the topic's or the keywords' intent), the
            # mismatch is resolved and the run proceeds as case (b).
            chosen = user_input.intent
            if chosen is not None and chosen in (topic_intent, keyword_intent):
                return DeciderOutcome(
                    case=AlignmentCase.b,
                    topic_intent=topic_intent,
                    keyword_intent=keyword_intent,
                    resolved_intent=chosen,
                    resolved_by="user_selected_intent",
                )
            return DeciderOutcome(
                case=AlignmentCase.c,
                topic_intent=topic_intent,
                keyword_intent=keyword_intent,
                resolved_intent=topic_intent,
                resolved_by="unresolved_mismatch",
            )

        # 4. Otherwise -> case (b).
        resolved = topic_intent if topic_intent != SearchIntent.mixed else keyword_intent
        return DeciderOutcome(
            case=AlignmentCase.b,
            topic_intent=topic_intent,
            keyword_intent=keyword_intent,
            resolved_intent=resolved,
            resolved_by="topic_and_keyword_alignment",
        )