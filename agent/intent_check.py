# pip install "pydantic>=2.0" "google-genai"
"""
Intent Alignment Check
======================
This module implements the Intent Alignment Check step of an SEO blog-generation pipeline.
It compares the search intent of a blog topic against the search intent of seed keywords
and determines whether the pipeline may proceed to keyword research.

Pause / Resume Contract:
------------------------
This module is strictly stateless. When `blocks_keyword_research` is True:
1. The caller inspects `result.message_to_user` and presents it to the user.
2. The caller gathers the user's response:
   - Updated topic and/or initial keywords
   - An explicitly chosen search intent (set on `UserInput.intent`)
   - Or an explicit override flag (`user_override = True`)
3. The caller increments `retry_count` on the `UserInput` instance (`user_input.retry_count += 1`).
4. The caller re-invokes `IntentAlignmentCheck.run(user_input)`.

Execution:
----------
User inputs are supplied via ``configure/config.yaml`` (project root),
under the ``blog_settings`` key.  There are no command-line arguments.

    python intent_check.py

The result is written to intent_result.json.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Optional, Protocol

try:
    from .decider import AlignmentDecider, DeciderOutcome
    from .enums import (
        Action,
        AlignmentCase,
        BusinessObjective,
        FunnelStage,
        SearchIntent,
    )
    from .models import AlignmentResult, ClassificationResult, UserInput
    from .prompts import (
        KEYWORD_SUGGESTION_SYSTEM_PROMPT,
        KEYWORD_SUGGESTION_USER_TEMPLATE,
        SYSTEM_PROMPT,
        USER_TEMPLATE,
    )
except ImportError:
    from decider import AlignmentDecider, DeciderOutcome
    from enums import (
        Action,
        AlignmentCase,
        BusinessObjective,
        FunnelStage,
        SearchIntent,
    )
    from models import AlignmentResult, ClassificationResult, UserInput
    from prompts import (
        KEYWORD_SUGGESTION_SYSTEM_PROMPT,
        KEYWORD_SUGGESTION_USER_TEMPLATE,
        SYSTEM_PROMPT,
        USER_TEMPLATE,
    )


def _load_env_file() -> None:
    """Load variables from .env file into os.environ if present, without external dependencies."""
    env_file = Path(__file__).resolve().parent / ".env"
    if env_file.is_file():
        with open(env_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, val = line.split("=", 1)
                    key = key.strip()
                    val = val.strip().strip("'\"")
                    if key and key not in os.environ and val:
                        os.environ[key] = val


_load_env_file()


# ==============================================================================
# LLM Layer
# ==============================================================================

class LLMClient(Protocol):
    """Protocol defining structured JSON generation for LLM clients."""
    def generate_json(self, system: str, user: str) -> dict[str, Any]:
        """Generate a structured JSON response as a dictionary."""
        ...


def extract_and_parse_json(raw_text: str) -> dict[str, Any]:
    """Extract and parse a JSON object from raw LLM output."""
    text = raw_text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()

    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
        raise ValueError(f"Expected JSON object, got {type(parsed).__name__}")
    except json.JSONDecodeError as exc:
        # Fallback: attempt to find outermost curly braces
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            sub = text[start : end + 1]
            parsed = json.loads(sub)
            if isinstance(parsed, dict):
                return parsed
        raise ValueError(f"Failed to parse JSON from text: {exc}") from exc


class GeminiClient:
    """Client for Google Gemini 3.6 via the google-genai SDK."""

    MODEL = "gemini-3.6-flash"

    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or os.environ.get("GEMINI_API_KEY")
        if not self.api_key:
            raise ValueError(
                "GEMINI_API_KEY is missing. Please set it in your .env file or environment."
            )
        self.model = self.MODEL

    def generate_json(self, system: str, user: str) -> dict[str, Any]:
        """Call Gemini 3.6 and return parsed JSON."""
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=self.api_key)
        config = types.GenerateContentConfig(
            system_instruction=system,
            response_mime_type="application/json",
            temperature=0.1,
        )
        response = client.models.generate_content(
            model=self.model,
            contents=user,
            config=config,
        )
        return extract_and_parse_json(response.text or "{}")


def get_llm_client(provider: Optional[str] = None) -> LLMClient:
    """Factory function returning the configured Gemini LLM client."""
    return GeminiClient()


# ==============================================================================
# IntentClassifier
# ==============================================================================

class ClassificationError(Exception):
    """Raised when classification fails or schema validation fails after retry."""
    pass


class IntentClassifier:
    """Handles LLM-based intent labeling for topics and keywords."""

    def __init__(self, llm_client: LLMClient):
        self.llm_client = llm_client

    def classify(self, user_input: UserInput) -> ClassificationResult:
        """Classify the topic and every seed keyword with one LLM call."""
        bullets = "\n".join(f"- {kw}" for kw in user_input.initial_keywords)
        declared = user_input.intent.value if user_input.intent else "none"
        user_content = USER_TEMPLATE.format(
            topic=user_input.topic,
            keywords=bullets,
            goal=user_input.goal,
            declared_intent=declared,
        )

        try:
            raw_json = self.llm_client.generate_json(system=SYSTEM_PROMPT, user=user_content)
            return ClassificationResult.model_validate(raw_json)
        except Exception as first_error:
            # Retry once with explicit schema reminder
            retry_user_content = (
                user_content
                + f"\n\n[System note: Your previous output failed schema validation ({first_error}). "
                "Ensure strict JSON conforming to the requested schema.]"
            )
            try:
                raw_json = self.llm_client.generate_json(system=SYSTEM_PROMPT, user=retry_user_content)
                return ClassificationResult.model_validate(raw_json)
            except Exception as second_error:
                raise ClassificationError(
                    f"Classification failed after retry: {second_error}"
                ) from second_error

    def suggest_keywords(self, topic: str, intent: SearchIntent) -> list[str]:
        """Suggest alternative seed keywords matching the specified intent."""
        user_content = KEYWORD_SUGGESTION_USER_TEMPLATE.format(
            topic=topic,
            intent=intent.value,
        )
        try:
            raw = self.llm_client.generate_json(
                system=KEYWORD_SUGGESTION_SYSTEM_PROMPT,
                user=user_content,
            )
            items = raw.get("suggested_keywords", [])
            if isinstance(items, list):
                return [str(k).strip() for k in items if str(k).strip()]
            return []
        except Exception:
            return []


class ResponseBuilder:
    """Maps decision outcomes and user inputs to the final AlignmentResult."""

    def build(
        self,
        user_input: UserInput,
        outcome: DeciderOutcome,
        classification: ClassificationResult,
        suggested_keywords: list[str],
    ) -> AlignmentResult:
        """Construct the complete AlignmentResult according to output rules."""
        case = outcome.case
        topic_intent = outcome.topic_intent
        keyword_intent = outcome.keyword_intent
        resolved_intent = outcome.resolved_intent
        resolved_by = outcome.resolved_by
        suggested = suggested_keywords

        if case == AlignmentCase.a:
            action = Action.PROMPT_SPECIFIC
            message = (
                "The topic or seed keywords do not have a clear search intent. "
                "Please provide a more specific topic or seed keywords before proceeding to Keyword Planner, "
                "or specify user override."
            )

        elif case == AlignmentCase.b:
            action = Action.PROCEED
            resolved_intent_str = (
                resolved_intent.value if hasattr(resolved_intent, "value") else str(resolved_intent)
            )
            if outcome.resolved_by == "user_override":
                message = f"User override active. Proceeding with resolved intent '{resolved_intent_str}'."
            elif outcome.resolved_by == "user_selected_intent":
                message = (
                    f"Intent mismatch resolved by user selection. Proceeding with '{resolved_intent_str}' "
                    "taking precedence."
                )
            else:
                message = f"Search intent is aligned ({resolved_intent_str}). Proceeding to keyword research."

        elif case == AlignmentCase.c:
            action = Action.SHOW_BOTH_INTENTS
            message = (
                f"Search intent mismatch: Topic intent is '{topic_intent.value}' while keyword intent is "
                f"'{keyword_intent.value}'. Please clarify which intent should take precedence, or select "
                f"from the suggested keywords matching the topic's intent."
            )

        elif case == AlignmentCase.d:
            # Case (d): No clear intent for either
            if user_input.retry_count < 1:
                action = Action.PROMPT_SPECIFIC
                message = (
                    "No clear search intent could be identified for either the topic or seed keywords. "
                    "Please provide a more specific seed keyword or topic."
                )
                resolved_intent = SearchIntent.unclear
                resolved_by = "unresolved"
            else:
                # retry_count >= 1: FALLBACK_FUNNEL_OBJECTIVE
                derived: Optional[SearchIntent] = None
                fallback_source: Optional[str] = None

                # 1. Derive from funnel_stage first
                if user_input.funnel_stage in (FunnelStage.awareness, FunnelStage.retention):
                    derived = SearchIntent.informational
                    fallback_source = "funnel_stage"
                elif user_input.funnel_stage == FunnelStage.consideration:
                    derived = SearchIntent.commercial
                    fallback_source = "funnel_stage"
                elif user_input.funnel_stage == FunnelStage.decision:
                    derived = SearchIntent.transactional
                    fallback_source = "funnel_stage"

                # 2. Then derive from business_objective
                if derived is None and user_input.business_objective is not None:
                    if user_input.business_objective in (
                        BusinessObjective.traffic,
                        BusinessObjective.brand_authority,
                        BusinessObjective.customer_education,
                    ):
                        derived = SearchIntent.informational
                        fallback_source = "business_objective"
                    elif user_input.business_objective == BusinessObjective.leads:
                        derived = SearchIntent.commercial
                        fallback_source = "business_objective"

                if derived is not None and fallback_source is not None:
                    action = Action.FALLBACK_FUNNEL_OBJECTIVE
                    resolved_intent = derived
                    resolved_by = fallback_source
                    message = (
                        f"Search intent was unclear after re-prompt. Successfully derived '{derived.value}' "
                        f"from {fallback_source.replace('_', ' ')}."
                    )
                else:
                    action = Action.PROMPT_SPECIFIC
                    resolved_intent = SearchIntent.unclear
                    resolved_by = "unresolved"
                    message = (
                        "No clear search intent could be identified and neither funnel stage nor business objective "
                        "is set. Please set a funnel stage / business objective, provide a more specific seed keyword, "
                        "or choose 'proceed anyway' (user override)."
                    )

        blocks_keyword_research = action in (Action.PROMPT_SPECIFIC, Action.SHOW_BOTH_INTENTS)

        return AlignmentResult(
            case=case,
            action=action,
            topic_intent=topic_intent,
            keyword_intent=keyword_intent,
            resolved_intent=resolved_intent,
            resolved_by=resolved_by,
            message_to_user=message,
            suggested_keywords=suggested,
            classification=classification,
            blocks_keyword_research=blocks_keyword_research,
        )


# ==============================================================================
# IntentAlignmentCheck (Orchestrator)
# ==============================================================================

class IntentAlignmentCheck:
    """Orchestrator coordinating classification, alignment decision, and response building."""

    def __init__(
        self,
        classifier: Optional[IntentClassifier] = None,
        decider: Optional[AlignmentDecider] = None,
        response_builder: Optional[ResponseBuilder] = None,
        llm_client: Optional[LLMClient] = None,
    ):
        if classifier is not None:
            self.classifier = classifier
        else:
            client = llm_client or get_llm_client()
            self.classifier = IntentClassifier(llm_client=client)

        self.decider = decider or AlignmentDecider()
        self.response_builder = response_builder or ResponseBuilder()

    def run(self, user_input: UserInput) -> AlignmentResult:
        """Execute the full intent alignment check on user input."""
        classification = self.classifier.classify(user_input)
        outcome = self.decider.decide(user_input, classification)

        suggested_keywords: list[str] = []
        if outcome.case == AlignmentCase.c:
            suggested_keywords = self.classifier.suggest_keywords(
                topic=user_input.topic,
                intent=classification.topic.intent,
            )

        return self.response_builder.build(
            user_input=user_input,
            outcome=outcome,
            classification=classification,
            suggested_keywords=suggested_keywords,
        )


# ==============================================================================
# Config-driven entrypoint
# ==============================================================================

# Single source of truth: configure/config.yaml, blog_settings section.
CONFIG_PATH = Path(__file__).resolve().parent.parent / "configure" / "config.yaml"
OUTPUT_PATH = "intent_result.json"


def main() -> None:
    """Entrypoint: load user input from configure/config.yaml (blog_settings), write intent_result.json."""
    import yaml  # PyYAML — already a project dependency via config_loader

    p = CONFIG_PATH
    if not p.is_file():
        raise SystemExit(f"Config file not found: '{p}'. Expected configure/config.yaml at project root.")

    with open(p, "r", encoding="utf-8") as f:
        try:
            raw = yaml.safe_load(f)
        except yaml.YAMLError as exc:
            raise SystemExit(f"Error parsing YAML in '{p}': {exc}")

    data = raw.get("blog_settings", {})
    if not data:
        raise SystemExit(f"'blog_settings' key missing or empty in '{p}'.")

    try:
        user_input = UserInput.model_validate(data)
    except Exception as exc:
        raise SystemExit(f"Invalid input in '{p}' [blog_settings]:\n{exc}")

    checker = IntentAlignmentCheck()
    result = checker.run(user_input)

    Path(OUTPUT_PATH).write_text(
        result.model_dump_json(indent=2), encoding="utf-8"
    )


if __name__ == "__main__":
    main()