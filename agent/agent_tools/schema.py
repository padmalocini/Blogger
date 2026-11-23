
from typing import List, Dict, Any, Optional, TypedDict, Annotated
from langgraph.graph.message import add_messages

# -----------------------------------------------------------------------------
# State definition for LangGraph
# -----------------------------------------------------------------------------

class BlogResearchState(TypedDict, total=False):

    ### Blog Research State
    messages: Annotated[list, add_messages]

    ### Blog Research Input
    keywords: List[str]
    user_topic: str
    goal: str
    format: str
    tone: str
    geo_zone: str
    target_audience: Any
    content_depth: str
    generate_topic_variations: bool
    message_to_convey: str
    content_pillar: str
    keyword_researcher: str

    ### Intent Alignment & Gating
    intent: Optional[str]
    funnel_stage: Optional[str]
    business_objective: Optional[str]
    user_override: bool
    retry_count: int
    resolved_intent: str
    intent_result: Optional[Dict[str, Any]]
    status: str
    error: Optional[str]
    case: Optional[str]
    action: Optional[str]
    message_to_user: Optional[str]
    suggested_keywords: Optional[List[str]]
    classification: Optional[Dict[str, Any]]

    ### Keyword Research Results
    keyword_result: Optional[Dict[str, Any]]
    run_dir: Optional[str]

    ### Blog Infers (Preserved for backward compatibility)
    topic: str
    primary_topic: List[str]
    blog_keywords: List[str]
    structure: str
    reading_level: str
    no_of_words: int                            

    ### Competitive Analysis (Preserved for backward compatibility)
    search_query: str
    search_results: List[str]
    analyzed_urls: List[Dict[str, Any]]
    reference_urls: List[str]
    html_contents: Dict[str, str]
    final_outline: str

    ### Blog Planning (Preserved for backward compatibility)
    planner_result: Any
    planner_state_plan: str
    plan_path: str
    user_choice: str


