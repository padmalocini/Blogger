from enum import Enum


class SearchIntent(str, Enum):
    """Search intent classification categories."""
    informational = "informational"
    commercial = "commercial"
    transactional = "transactional"
    navigational = "navigational"
    career = "career"
    mixed = "mixed"
    unclear = "unclear"


class FunnelStage(str, Enum):
    """Marketing funnel stages."""
    awareness = "awareness"
    consideration = "consideration"
    decision = "decision"
    retention = "retention"


class BusinessObjective(str, Enum):
    """Business objectives for the content."""
    traffic = "traffic"
    leads = "leads"
    brand_authority = "brand_authority"
    customer_education = "customer_education"


class ReaderMaturity(str, Enum):
    """Awareness/maturity stage of the target reader."""
    unaware = "unaware"
    problem_aware = "problem_aware"
    solution_aware = "solution_aware"
    product_aware = "product_aware"
    service_aware = "service_aware"


class AlignmentCase(str, Enum):
    """Outcome cases of the alignment decision."""
    a = "a"
    b = "b"
    c = "c"
    d = "d"


class Action(str, Enum):
    """Action dictated by alignment evaluation."""
    PROMPT_SPECIFIC = "PROMPT_SPECIFIC"
    PROCEED = "PROCEED"
    SHOW_BOTH_INTENTS = "SHOW_BOTH_INTENTS"
    FALLBACK_FUNNEL_OBJECTIVE = "FALLBACK_FUNNEL_OBJECTIVE"