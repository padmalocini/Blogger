# ------imports-------
import os
import json
import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Dict, Any, Optional

# ------- LangGraph imports -------
from langgraph.graph import StateGraph, END
from dotenv import load_dotenv

# -------------------------------------------
# Agent Working Modules imports
# -------------------------------------------
from .prompts import PromptTemplates
from .agent_tools.schema import BlogResearchState
from configure.config_loader import load_config
from configure.logger import setup_logger

prompt_gen = PromptTemplates()

# -------------------------------------------
# Setup logging
# -------------------------------------------
load_dotenv()
CONFIG = load_config()
logger = setup_logger(name="blog_writter", tag="BLOG_AGENT")





# -------------------------------------------
# Blog Writer Agent
# -------------------------------------------
class BlogWritterAgent:
    """Agent for blog research tasks.

    Current graph (Planner LLM removed):
        intent_check_node
            ├── [blocked]  → END   (returns status="blocked"; caller shows message_to_user)
            └── [proceed]  → keyword_research_node → END
    """

    def __init__(self):
        self.prompt_gen = PromptTemplates()

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    async def __call__(self, initial_state: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Run the complete blog research workflow."""

        if initial_state is None:
            bs = CONFIG["blog_settings"]
            initial_state = {
                "keywords":                 bs["initial_keywords"],
                "user_topic":               bs["topic"],
                "goal":                     bs["goal"],
                "format":                   bs["format"],
                "tone":                     bs["tone"],
                "geo_zone":                 bs.get("geo_zone", "global"),
                "target_audience":          bs.get("target_audience"),
                "content_depth":            bs.get("content_depth"),
                "generate_topic_variations":bs.get("generate_topic_variations"),
                "message_to_convey":        bs.get("message_to_convey"),
                "content_pillar":           bs.get("content_pillar"),
                "keyword_researcher":       bs.get("keyword_researcher", "google_ads"),
                "intent":                   bs.get("intent"),
                "funnel_stage":             bs.get("funnel_stage"),
                "business_objective":       bs.get("business_objective"),
                "user_override":            bool(bs.get("user_override", False)),
                "retry_count":              int(bs.get("retry_count", 0)),
                "status":                   "pending",
                "intent_result":            None,
                "keyword_result":           None,
            }
        else:
            defaults = {
                "keywords": [],
                "user_topic": "",
                "goal": "",
                "format": "",
                "tone": "",
                "geo_zone": "global",
                "target_audience": None,
                "content_depth": None,
                "generate_topic_variations": None,
                "message_to_convey": None,
                "content_pillar": None,
                "keyword_researcher": "google_ads",
                "intent": None,
                "funnel_stage": None,
                "business_objective": None,
                "user_override": False,
                "retry_count": 0,
                "status": "pending",
                "intent_result": None,
                "keyword_result": None,
            }
            for k, v in defaults.items():
                if k not in initial_state:
                    initial_state[k] = v

        app = self.create_blog_research_graph()
        final_state = await app.ainvoke(initial_state)
        return final_state

    # ------------------------------------------------------------------
    # Node 1: Intent Alignment Check
    # ------------------------------------------------------------------

    async def intent_check_node(self, state: BlogResearchState) -> BlogResearchState:

        from .intent_check import (
            IntentAlignmentCheck,
            UserInput,
        )

        keywords = state.get("keywords") or []
        topic    = (state.get("user_topic") or "").strip()

        # Build UserInput — Pydantic class handles enum coercion, audience joining, and boolean variations
        user_input = UserInput(
            topic=topic,
            initial_keywords=keywords,
            goal=state.get("goal", ""),
            format=state.get("format", ""),
            tone=state.get("tone", ""),
            target_audience=state.get("target_audience"),
            content_depth=state.get("content_depth"),
            generate_topic_variations=state.get("generate_topic_variations"),
            geo_zone=state.get("geo_zone", "global"),
            message_to_convey=state.get("message_to_convey"),
            content_pillar=state.get("content_pillar"),
            intent=state.get("intent"),
            funnel_stage=state.get("funnel_stage"),
            business_objective=state.get("business_objective"),
            user_override=bool(state.get("user_override", False)),
            retry_count=int(state.get("retry_count", 0)),
        )

        # Run the synchronous intent check off the event loop ---------------
        try:
            result = await asyncio.to_thread(IntentAlignmentCheck().run, user_input)
        except Exception as exc:
            logger.exception(f"intent_check_node: IntentAlignmentCheck raised: {exc}")
            return {
                **state,
                "status": "error",
                "error": f"Intent alignment check failed: {exc}",
            }

        case_val = result.case.value if hasattr(result.case, "value") else (str(result.case) if result.case is not None else None)
        action_val = result.action.value if hasattr(result.action, "value") else (str(result.action) if result.action is not None else None)
        resolved_val = result.resolved_intent.value if hasattr(result.resolved_intent, "value") else (str(result.resolved_intent) if result.resolved_intent is not None else None)
        classification_val = result.classification.model_dump(mode="json") if hasattr(result.classification, "model_dump") else (result.classification or {})

        logger.info(
            f"intent_check_node: case={case_val} action={action_val} "
            f"blocks={result.blocks_keyword_research} resolved_intent={resolved_val}"
        )

        # Shared state update ------------------------------------------------
        base_update = {
            **state,
            "intent_result":    result.model_dump(mode="json") if hasattr(result, "model_dump") else {},
            "resolved_intent":  resolved_val,
            "case":             case_val,
            "action":           action_val,
            "message_to_user":  result.message_to_user,
            "suggested_keywords": result.suggested_keywords,
            "classification":   classification_val,
        }

        if result.blocks_keyword_research:
            logger.info("intent_check_node: keyword research BLOCKED.")
            return {
                **base_update,
                "status": "blocked",
            }
        else:
            logger.info("intent_check_node: intent aligned — proceeding to keyword research.")
            return {
                **base_update,
                "status": "proceed",
            }

    # ------------------------------------------------------------------
    # Routing: decides next node after intent_check_node
    # ------------------------------------------------------------------

    @staticmethod
    def _route_after_intent(state: BlogResearchState) -> str:
        status = state.get("status", "")
        if status == "proceed":
            return "keyword_research_node"
        # blocked | error → END
        return END

    # ------------------------------------------------------------------
    # Node 2: Keyword Research (runs ONLY when status == "proceed")
    # ------------------------------------------------------------------

    async def keyword_research_node(self, state: BlogResearchState) -> BlogResearchState:
        """
        Fetches, scores, and tiers keywords for every seed supplied by the user.

        No keyword data is sent to any LLM.
        Writes <run_dir>/keyword_result.json and <run_dir>/scored_keywords.csv.
        """
        from .keyword_research import (
            fetch_scored_keywords,
            MIN_KEYWORDS_REQUIRED,
            report_spec_for,
            AGENT_COLUMNS,
        )

        # ------------------------------------------------------------------
        # Create run directory & pre-seed audit CSV template
        # ------------------------------------------------------------------
        run_dir = state.get("run_dir")
        if not run_dir:
            current_time = datetime.now(ZoneInfo("Asia/Kolkata"))
            date_folder  = current_time.strftime("%d_%m_%Y")
            time_folder  = current_time.strftime("%H_%M_%S")
            run_dir = os.path.join(os.getcwd(), "output", date_folder, time_folder)
            os.makedirs(run_dir, exist_ok=True)

        researcher_type = state.get("keyword_researcher", "google_ads")
        report_filename, report_columns = report_spec_for(researcher_type)
        try:
            with open(os.path.join(run_dir, report_filename), "w", encoding="utf-8") as f:
                f.write(",".join(report_columns) + "\n")
        except Exception as seed_err:
            logger.error(f"keyword_research_node: failed pre-seeding CSV template: {seed_err}")

        # ------------------------------------------------------------------
        # Fetch the full scored keyword pool (no 50-row LLM-prompt cap)
        # ------------------------------------------------------------------
        seed_keywords = state.get("keywords", [])
        geo_zone      = state.get("geo_zone", "us")

        try:
            merged_df = await asyncio.to_thread(
                fetch_scored_keywords,
                seed_keywords,
                researcher_type,
                geo_zone,
                run_dir,
            )
        except Exception as exc:
            logger.exception(f"keyword_research_node: fetch_scored_keywords raised: {exc}")
            return {
                **state,
                "status": "error",
                "error": f"Keyword fetch failed: {exc}",
                "run_dir": run_dir,
            }

        total_found = len(merged_df)

        # Minimum-keyword guard ---------------------------------------------
        if total_found < MIN_KEYWORDS_REQUIRED:
            msg = (
                f"Keyword research returned only {total_found} keyword(s) for "
                f"{', '.join(seed_keywords)}. At least {MIN_KEYWORDS_REQUIRED} are needed. "
                f"Try broader or more complete seed keywords."
            )
            logger.error(f"keyword_research_node: {msg}")
            return {
                **state,
                "status": "insufficient_keywords",
                "error": msg,
                "run_dir": run_dir,
            }

        # ------------------------------------------------------------------
        # Sort by score, group by tier, build output dict
        # ------------------------------------------------------------------
        df_sorted = merged_df.sort_values("score", ascending=False)

        tier_counts = df_sorted["tier"].value_counts().to_dict()
        tier_counts = {"A": tier_counts.get("A", 0),
                       "B": tier_counts.get("B", 0),
                       "C": tier_counts.get("C", 0)}

        keywords_payload = df_sorted[AGENT_COLUMNS].to_dict(orient="records")

        intent_result = state.get("intent_result") or {}
        keyword_result = {
            "status":               "ok",
            "resolved_intent":      state.get("resolved_intent", ""),
            "resolved_by":          intent_result.get("resolved_by", ""),
            "geo_zone":             geo_zone,
            "researcher":           researcher_type,
            "seed_keywords":        seed_keywords,
            "total_keywords_found": total_found,
            "tier_counts":          tier_counts,
            "keywords":             keywords_payload,
            "intent_check": {
                "case":            state.get("case", ""),
                "action":          state.get("action", ""),
                "message_to_user": state.get("message_to_user", ""),
            },
        }

        # ------------------------------------------------------------------
        # Persist keyword_result.json
        # ------------------------------------------------------------------
        result_json_path = os.path.join(run_dir, "keyword_result.json")
        try:
            with open(result_json_path, "w", encoding="utf-8") as f:
                json.dump(keyword_result, f, indent=2, ensure_ascii=False)
            logger.info(f"keyword_research_node: keyword_result.json → {result_json_path}")
        except Exception as write_err:
            logger.error(f"keyword_research_node: failed writing keyword_result.json: {write_err}")

        # ------------------------------------------------------------------
        # Persist scored_keywords.csv (full pool, all tiers)
        # ------------------------------------------------------------------
        scored_csv_path = os.path.join(run_dir, "scored_keywords.csv")
        try:
            df_sorted[AGENT_COLUMNS].to_csv(scored_csv_path, index=False)
            logger.info(f"keyword_research_node: scored_keywords.csv → {scored_csv_path}")
        except Exception as csv_err:
            logger.error(f"keyword_research_node: failed writing scored_keywords.csv: {csv_err}")

        logger.info(
            f"keyword_research_node: {total_found} keywords found — "
            f"A={tier_counts['A']} B={tier_counts['B']} C={tier_counts['C']}"
        )

        return {
            **state,
            "status":         "ok",
            "keyword_result": keyword_result,
            "run_dir":        run_dir,
        }

    # ------------------------------------------------------------------
    # Graph builder
    # ------------------------------------------------------------------

    def create_blog_research_graph(self) -> StateGraph:
        
        workflow = StateGraph(BlogResearchState)

        workflow.add_node("intent_check_node",     self.intent_check_node)
        workflow.add_node("keyword_research_node", self.keyword_research_node)

        workflow.set_entry_point("intent_check_node")

        workflow.add_conditional_edges(
            "intent_check_node",
            self._route_after_intent,
            {
                "keyword_research_node": "keyword_research_node",
                END: END,
            },
        )

        workflow.add_edge("keyword_research_node", END)

        return workflow.compile()
