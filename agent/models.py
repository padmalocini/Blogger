from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, field_validator

try:
    from .enums import (
        Action,
        AlignmentCase,
        BusinessObjective,
        FunnelStage,
        ReaderMaturity,
        SearchIntent,
    )
except ImportError:
    from enums import (
        Action,
        AlignmentCase,
        BusinessObjective,
        FunnelStage,
        ReaderMaturity,
        SearchIntent,
    )


class UserInput(BaseModel):
    """Input payload for blog generation pipeline intent check."""
    # required
    topic: str
    initial_keywords: list[str] = Field(min_length=1)
    goal: str
    format: str
    tone: str

    # optional
    target_audience: Optional[str] = None
    content_depth: Optional[str] = None
    generate_topic_variations: Optional[bool] = False
    geo_zone: Optional[str] = "global"
    message_to_convey: Optional[str] = None
    content_pillar: Optional[str] = None
    intent: Optional[SearchIntent] = None
    business_objective: Optional[BusinessObjective] = None
    funnel_stage: Optional[FunnelStage] = None
    reader_maturity: Optional[ReaderMaturity] = None
    expected_outcome: Optional[str] = None

    # run state
    user_override: bool = False
    retry_count: int = 0

    @field_validator("topic")
    @classmethod
    def validate_topic(cls, value: str) -> str:
        """Strip and validate that topic is non-empty."""
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("Topic cannot be empty or whitespace only.")
        return cleaned

    @field_validator("initial_keywords")
    @classmethod
    def validate_initial_keywords(cls, value: list[str]) -> list[str]:
        """Strip and de-duplicate initial keywords, preserving order."""
        seen: set[str] = set()
        cleaned: list[str] = []
        for item in value:
            stripped = item.strip()
            if stripped and stripped not in seen:
                seen.add(stripped)
                cleaned.append(stripped)
        if not cleaned:
            raise ValueError("initial_keywords must contain at least one non-empty keyword.")
        return cleaned

    @field_validator("intent", "funnel_stage", "business_objective", "reader_maturity", mode="before")
    @classmethod
    def validate_enums_safely(cls, value: Any, info) -> Any:
        """Safely parse string values into enums using Pydantic; ignore unrecognised values."""
        if value is None or value == "":
            return None
        enum_map = {
            "intent": SearchIntent,
            "funnel_stage": FunnelStage,
            "business_objective": BusinessObjective,
            "reader_maturity": ReaderMaturity,
        }
        enum_cls = enum_map.get(info.field_name)
        if enum_cls is not None:
            if isinstance(value, enum_cls):
                return value
            try:
                return enum_cls(value)
            except (ValueError, KeyError):
                return None
        return value

    @field_validator("target_audience", mode="before")
    @classmethod
    def validate_target_audience(cls, value: Any) -> Optional[str]:
        """Convert list of target audiences to comma-separated string."""
        if isinstance(value, list):
            return ", ".join(str(x) for x in value) if value else None
        return str(value) if value is not None else None

    @field_validator("generate_topic_variations", mode="before")
    @classmethod
    def validate_generate_topic_variations(cls, value: Any) -> bool:
        """Normalize integers, strings, or booleans to a bool."""
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value > 0)
        if isinstance(value, str):
            return value.strip().lower() in ("true", "1", "yes")
        if value is None:
            return False
        return bool(value)


class IntentLabel(BaseModel):
    """Intent classification label for a single topic or keyword."""
    text: str
    intent: SearchIntent
    secondary_intent: Optional[SearchIntent] = None
    ambiguous: bool
    confidence: Literal["high", "medium", "low"]
    reasoning: str


class ClassificationResult(BaseModel):
    """Complete classification output covering topic and keywords."""
    topic: IntentLabel
    keywords: list[IntentLabel]


class AlignmentResult(BaseModel):
    """Final decision and status of the intent alignment check."""
    case: AlignmentCase
    action: Action
    topic_intent: SearchIntent
    keyword_intent: SearchIntent
    resolved_intent: SearchIntent
    resolved_by: str
    message_to_user: str
    suggested_keywords: list[str] = Field(default_factory=list)
    classification: ClassificationResult
    blocks_keyword_research: bool