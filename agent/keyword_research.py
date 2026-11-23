# Mode: Engineering Mode
# Principles Applied: Single Responsibility, Interface Segregation, Protocol Stdio Safety, Audit Logging Isolation

import os
import sys
import math
import json
import logging
import http.client
from urllib.parse import quote
import pandas as pd
from typing import List, Optional
from dotenv import load_dotenv

# Google Ads SDK client imports
# google-ads SDK is optional; imported lazily inside functions that use it
# so the module loads cleanly in environments without the SDK installed.

# Redirect standard logging to stderr to prevent stdio protocol corruption
logging.basicConfig(
    level=logging.INFO, 
    stream=sys.stderr, 
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("mcp_google_ads_research")

# Location mapping: geoTargetConstants IDs from Google Ads API.
# Keys cover every spelling the UI and the planner prompt may send, so a user typing
# "india" gets Indian data instead of silently falling through to the US default.
LOCATIONS = {
    "us": "2840", "usa": "2840", "united states": "2840",
    "in": "2356", "india": "2356",
    "uk": "2826", "gb": "2826", "united kingdom": "2826",
    "ca": "2124", "canada": "2124",
    "au": "2036", "australia": "2036",
    "de": "2276", "germany": "2276",
    "fr": "2250", "france": "2250",
    "sg": "2702", "singapore": "2702",
    # "global" has no geoTargetConstant — Google requires a country. US is used as the
    # worldwide-English proxy, and this is stated in the UI and the planner prompt so the
    # data source is never misrepresented.
    "global": "2840",
}

# Country codes the RapidAPI backend accepts, keyed the same way as LOCATIONS so both
# sources resolve identical user input to the same country.
RAPIDAPI_COUNTRIES = {
    "us": "us", "usa": "us", "united states": "us", "global": "us",
    "in": "in", "india": "in",
    "uk": "uk", "gb": "uk", "united kingdom": "uk",
    "ca": "ca", "canada": "ca",
    "au": "au", "australia": "au",
    "de": "de", "germany": "de",
    "fr": "fr", "france": "fr",
    "sg": "sg", "singapore": "sg",
}


def resolve_location(geo_zone: Optional[str]) -> str:
    """Maps a user-supplied zone to a geoTargetConstant, logging any unrecognised value."""
    zone = (geo_zone or "us").strip().lower()
    if zone not in LOCATIONS:
        logger.warning(
            f"Unrecognised geo_zone '{geo_zone}' — no Google Ads location matches it. "
            f"Falling back to United States. Supported: {sorted(set(LOCATIONS))}"
        )
    return LOCATIONS.get(zone, "2840")

# Audit CSV schemas — single source of truth, shared with the pre-seeding in blog_agent.py
# so a placeholder file can never drift from what a real fetch writes.
# Bid columns carry no currency suffix: Google Ads reports them in the *account's* currency
# (INR here), so a "_usd" label misstates the data. They are informational only — scoring
# uses search volume and competition alone.
GOOGLE_ADS_REPORT_COLUMNS = [
    "keyword", "avg_monthly_searches", "competition_level",
    "competition_index", "low_top_of_page_bid",
    "high_top_of_page_bid", "average_cpc",
    "monthly_search_history", "score", "tier"
]
RAPIDAPI_REPORT_COLUMNS = ["text", "vol", "competition", "score", "tier"]

# Columns handed to the planner. Bid/CPC is deliberately absent — it is not used in scoring
# and is reported in account currency, so exposing it to the model invites false reasoning.
AGENT_COLUMNS = ["text", "vol", "competition", "score", "tier"]

REPORT_SPECS = {
    "google_ads": ("google_ads_keyword_report.csv", GOOGLE_ADS_REPORT_COLUMNS),
    "rapidapi":   ("rapidapi_keyword_report.csv",   RAPIDAPI_REPORT_COLUMNS),
}


def report_spec_for(researcher_type: str) -> tuple:
    """Returns (csv_filename, column_list) for the selected keyword researcher."""
    return REPORT_SPECS.get(researcher_type.lower(), REPORT_SPECS["google_ads"])


def load_env_if_present():
    """Locates and loads .env configuration from the execution context paths."""
    cwd = os.getcwd()
    paths_to_check = [
        os.path.join(cwd, ".env")
    ]
    for path in paths_to_check:
        if os.path.exists(path):
            load_dotenv(path)
            logger.info(f"Loaded environment parameters from: {path}")
            return
    logger.warning("No .env configuration file detected. Using active system env.")

def get_google_ads_client():
    """Instantiates Google Ads client using credentials loaded from the environment."""
    try:
        from google.ads.googleads.client import GoogleAdsClient  # lazy import
    except ImportError as exc:
        raise ImportError(
            "google-ads SDK is not installed. Run: pip install google-ads"
        ) from exc
    required_variables = [
        "GOOGLE_ADS_DEVELOPER_TOKEN",
        "GOOGLE_ADS_CLIENT_ID",
        "GOOGLE_ADS_CLIENT_SECRET",
        "GOOGLE_ADS_REFRESH_TOKEN",
        "GOOGLE_ADS_CUSTOMER_ID"
    ]
    
    missing = [var for var in required_variables if not os.getenv(var)]
    if missing:
        raise ValueError(
            f"Missing required Google Ads environment keys: {', '.join(missing)}. "
            "Please ensure they are defined in your .env file."
        )

    credentials = {
        "developer_token": os.getenv("GOOGLE_ADS_DEVELOPER_TOKEN"),
        "client_id":       os.getenv("GOOGLE_ADS_CLIENT_ID"),
        "client_secret":   os.getenv("GOOGLE_ADS_CLIENT_SECRET"),
        "refresh_token":   os.getenv("GOOGLE_ADS_REFRESH_TOKEN"),
        "use_proto_plus":  True
    }
    
    login_customer_id = os.getenv("GOOGLE_ADS_LOGIN_CUSTOMER_ID")
    if login_customer_id:
        credentials["login_customer_id"] = login_customer_id.replace("-", "")

    from google.ads.googleads.client import GoogleAdsClient  # lazy import (already checked above)
    return GoogleAdsClient.load_from_dict(credentials)

def _competition_label(value) -> str:
    """Normalizes raw Google Ads competition enum names to agent-compatible strings."""
    val_str = str(value).upper()
    mapping = {"LOW": "Low", "MEDIUM": "Medium", "HIGH": "High"}
    return mapping.get(val_str, "Unknown")


def _score_and_tier(vol: int, competition_label: str, competition_index: Optional[int] = None) -> tuple:
    """
    Opportunity score (0-10) from search volume and competition ONLY — currency-independent,
    unlike the previous cpc-based formula (which broke on non-USD Google Ads accounts).
    """
    volume_signal = min(1.0, math.log10(vol + 1) / 5.0)

    if competition_index is not None and competition_label != "Unknown":
        competition_signal = 1.0 - (competition_index / 100.0)
    else:
        competition_signal = {"Low": 0.85, "Medium": 0.5, "High": 0.15}.get(competition_label, 0.5)

    score = round(10 * (0.6 * volume_signal + 0.4 * competition_signal), 2)

    if score >= 6.5 and competition_label in ("Low", "Medium"):
        tier = "A"
    elif score >= 4.0:
        tier = "B"
    else:
        tier = "C"

    return score, tier

def fetch_google_ads_keywords(
    keyword: str,
    location_code: str,
    language_code: str = "1000"
) -> List[dict]:
    """Queries the Google Ads KeywordPlanIdeaService for keyword suggestions and returns raw stats."""
    client = get_google_ads_client()
    customer_id = os.getenv("GOOGLE_ADS_CUSTOMER_ID", "").replace("-", "")
    
    service = client.get_service("KeywordPlanIdeaService")
    request = client.get_type("GenerateKeywordIdeasRequest")

    request.customer_id = customer_id
    request.language = f"languageConstants/{language_code}"
    request.geo_target_constants.append(f"geoTargetConstants/{location_code}")
    request.keyword_plan_network = client.enums.KeywordPlanNetworkEnum.GOOGLE_SEARCH
    request.include_adult_keywords = False

    # Attach keyword seed
    seed = client.get_type("KeywordSeed")
    seed.keywords.append(keyword)
    request.keyword_seed = seed

    response = service.generate_keyword_ideas(request=request)
    keyword_list = []
    
    for row in response.results:
        metrics = row.keyword_idea_metrics
        
        # Convert micro-bid values (millionths of a unit) to standard currency float
        low_bid = round(metrics.low_top_of_page_bid_micros / 1_000_000, 2) if metrics.low_top_of_page_bid_micros else 0.0
        high_bid = round(metrics.high_top_of_page_bid_micros / 1_000_000, 2) if metrics.high_top_of_page_bid_micros else 0.0
        raw_cpc = round(metrics.average_cpc_micros / 1_000_000, 2) if metrics.average_cpc_micros else 0.0
        
        # Serialize 12-month historical volumes as a list
        history = []
        if metrics.monthly_search_volumes:
            for vol_entry in metrics.monthly_search_volumes:
                history.append({
                    "year": vol_entry.year,
                    "month": str(vol_entry.month.name) if hasattr(vol_entry.month, 'name') else str(vol_entry.month),
                    "volume": vol_entry.monthly_searches
                })
        
        keyword_list.append({
            "keyword": row.text,
            "avg_monthly_searches": metrics.avg_monthly_searches or 0,
            "competition_level": str(metrics.competition.name) if hasattr(metrics.competition, 'name') else str(metrics.competition),
            "competition_index": metrics.competition_index or 0,
            "low_top_of_page_bid": low_bid,
            "high_top_of_page_bid": high_bid,
            "average_cpc": raw_cpc,
            "monthly_search_history": json.dumps(history)
        })

    return keyword_list

def _fetch_google_ads_keywords(keyword: str, geo_zone: Optional[str] = None, output_dir: Optional[str] = None) -> pd.DataFrame:
    """Performs keyword discovery via Google Ads API."""
    agent_headers = AGENT_COLUMNS
    try:
        load_env_if_present()

        # Resolve target location — explicit param takes precedence over env (avoids
        # concurrency races when called in-process instead of via an isolated subprocess)
        location_code = resolve_location(geo_zone or os.getenv("GEO_ZONE", "us"))

        raw_keywords = fetch_google_ads_keywords(
            keyword=keyword,
            location_code=location_code
        )

        processed_rows = []
        for item in raw_keywords:
            vol = item["avg_monthly_searches"]

            # Normalize competition text casing
            competition = _competition_label(item["competition_level"])

            # Opportunity score: search volume + competition only (currency-independent)
            score, tier = _score_and_tier(vol, competition, item["competition_index"])

            processed_rows.append({
                # Raw Google Ads insights — bids are informational, in account currency
                "keyword": item["keyword"],
                "avg_monthly_searches": vol,
                "competition_level": item["competition_level"],
                "competition_index": item["competition_index"],
                "low_top_of_page_bid": item["low_top_of_page_bid"],
                "high_top_of_page_bid": item["high_top_of_page_bid"],
                "average_cpc": item["average_cpc"],
                "monthly_search_history": item["monthly_search_history"],

                # Agent Calculation Columns (Mapped to expected keys)
                "text": item["keyword"],
                "vol": vol,
                "competition": competition,
                "score": score,
                "tier": tier
            })
            
        full_df = pd.DataFrame(processed_rows)
        
        if full_df.empty:
            logger.warning("Google Ads API returned zero keyword results.")
            return pd.DataFrame(columns=agent_headers)
            
        # Export expanded CSV report containing all 8 raw insights
        try:
            output_dir = output_dir or os.getenv("RUN_OUTPUT_DIR") or os.path.join(os.getcwd(), "output")
            os.makedirs(output_dir, exist_ok=True)
            log_path = os.path.join(output_dir, "google_ads_keyword_report.csv")

            # Filter columns specifically for the audit report
            new_report_df = full_df[GOOGLE_ADS_REPORT_COLUMNS]
            
            if os.path.exists(log_path):
                try:
                    existing_df = pd.read_csv(log_path)
                    existing_df = existing_df.dropna(subset=["keyword"])
                    combined_df = pd.concat([existing_df, new_report_df], ignore_index=True)
                    new_report_df = combined_df.drop_duplicates(subset=["keyword"])
                except Exception as read_err:
                    logger.error(f"Error reading existing CSV: {read_err}")

            new_report_df.to_csv(log_path, index=False)
            logger.info(f"Local verification report written successfully to: {log_path}")
        except Exception as file_err:
            logger.error(f"Failed writing local verification copy: {str(file_err)}")
            
        # Slices out only the 5 required columns to return to the AI agent
        agent_df = full_df[agent_headers]
        return agent_df

    except Exception as gae_or_generic:
        # Catch GoogleAdsException (lazy import) or any other error
        try:
            from google.ads.googleads.errors import GoogleAdsException
            if isinstance(gae_or_generic, GoogleAdsException):
                gae = gae_or_generic
                error_details = []
                for error in gae.failure.errors:
                    trigger = error.trigger.string_value if hasattr(error.trigger, 'string_value') else ''
                    error_details.append(f"{error.message} (trigger: {trigger})")
                logger.error(f"Google Ads API Call Failed: {'; '.join(error_details)}")
                return pd.DataFrame(columns=agent_headers)
        except ImportError:
            pass
        logger.exception(f"Unhandled exception during Google Ads keyword tool call: {str(gae_or_generic)}")
        return pd.DataFrame(columns=agent_headers)


def _fetch_rapidapi_keywords(keyword: str, geo_zone: Optional[str] = None, output_dir: Optional[str] = None) -> pd.DataFrame:
    """Perform keyword research using the SEO Keyword Research API from prod branch."""
    agent_headers = AGENT_COLUMNS
    try:
        load_env_if_present()
        rapidapi_key = os.getenv("RAPIDAPI_KEY", "")
        if not rapidapi_key:
            logger.error("RAPIDAPI_KEY is not set — add it to .env to use the RapidAPI source.")
            return pd.DataFrame(columns=agent_headers)

        encoded_keyword = quote(keyword)

        conn = http.client.HTTPSConnection("seo-keyword-research.p.rapidapi.com")
        headers = {
            'x-rapidapi-key': rapidapi_key,
            'x-rapidapi-host': "seo-keyword-research.p.rapidapi.com"
        }

        # Same zone vocabulary as the Google Ads path, so identical input hits the same country
        zone = (geo_zone or os.getenv("GEO_ZONE", "us")).strip().lower()
        if zone not in RAPIDAPI_COUNTRIES:
            logger.warning(f"Unrecognised geo_zone '{geo_zone}' for RapidAPI — falling back to 'us'.")
        country = RAPIDAPI_COUNTRIES.get(zone, "us")
        path = f"/keynew.php?keyword={encoded_keyword}&country={country}"

        logger.info(f"RapidAPI request: GET {path}")
        conn.request("GET", path, headers=headers)

        res = conn.getresponse()
        data = res.read()
        decoded_data = data.decode("utf-8")

        # A failed call still returns valid JSON (e.g. {"message": "You are not subscribed..."}),
        # so without this check a subscription problem looks identical to "no keywords found".
        if res.status != 200:
            logger.error(f"RapidAPI returned HTTP {res.status} {res.reason}: {decoded_data[:300]}")
            return pd.DataFrame(columns=agent_headers)

        try:
            json_data = json.loads(decoded_data)
        except json.JSONDecodeError:
            logger.error(f"RapidAPI returned non-JSON response: {decoded_data[:300]}")
            return pd.DataFrame(columns=agent_headers)

        if isinstance(json_data, dict):
            logger.error(f"RapidAPI returned an error object instead of keywords: {json_data}")
            return pd.DataFrame(columns=agent_headers)

        if not json_data:
            logger.warning("RapidAPI returned empty results.")
            return pd.DataFrame(columns=agent_headers)

        full_df = pd.DataFrame(json_data)

        # Data coercion and normalization to agent_headers
        if "text" not in full_df.columns:
            logger.error("RapidAPI response missing required 'text' column.")
            return pd.DataFrame(columns=agent_headers)

        if "vol" not in full_df.columns:
            full_df["vol"] = 0
        else:
            full_df["vol"] = pd.to_numeric(full_df["vol"], errors='coerce').fillna(0).astype(int)
            
        if "competition" not in full_df.columns:
            full_df["competition"] = "Unknown"
        else:
            # Normalize casing (RapidAPI returns lowercase "low"/"medium"/"high")
            full_df["competition"] = full_df["competition"].fillna("Unknown").apply(_competition_label)

        # Opportunity score: volume + competition only (matches the Google Ads formula —
        # RapidAPI has no competition_index, so competition_signal falls back to the label map)
        scored = full_df.apply(lambda row: _score_and_tier(row["vol"], row["competition"]), axis=1)
        full_df["score"] = [s for s, _ in scored]
        full_df["tier"] = [t for _, t in scored]

        # Write validation CSV report locally for auditing
        try:
            output_dir = output_dir or os.getenv("RUN_OUTPUT_DIR") or os.path.join(os.getcwd(), "output")
            os.makedirs(output_dir, exist_ok=True)
            log_path = os.path.join(output_dir, "rapidapi_keyword_report.csv")
            target_df = full_df[RAPIDAPI_REPORT_COLUMNS]
            
            if os.path.exists(log_path):
                try:
                    existing_df = pd.read_csv(log_path)
                    existing_df = existing_df.dropna(subset=["text"])
                    combined_df = pd.concat([existing_df, target_df], ignore_index=True)
                    target_df = combined_df.drop_duplicates(subset=["text"])
                except Exception as read_err:
                    logger.error(f"Error reading existing CSV: {read_err}")
                    
            target_df.to_csv(log_path, index=False)
            logger.info(f"Local verification report written successfully to: {log_path}")
        except Exception as file_err:
            logger.error(f"Failed writing local verification copy: {str(file_err)}")
            
        return full_df[agent_headers]

    except Exception as e:
        logger.exception(f"RapidAPI failed: {e}")
        return pd.DataFrame(columns=agent_headers)


# Minimum keywords required to proceed to downstream steps.
# Past experience shows that below this count fabricated rows appear to satisfy schemas.
MIN_KEYWORDS_REQUIRED = 10

# Backward-compatible alias — existing imports of MIN_KEYWORDS_FOR_PLANNING still work.
MIN_KEYWORDS_FOR_PLANNING = MIN_KEYWORDS_REQUIRED


def select_keywords_for_prompt(df: pd.DataFrame, max_rows: int = 50) -> List[dict]:
    """
    Tier-balanced, JSON-ready subset of keyword rows for direct prompt injection.

    Tier A leads, Tier B fills the middle, and Tier C keeps a reserved block of slots so the
    plan always has long-tail phrasing to build section headings from. Broad topics return
    hundreds of Tier A keywords, so the Tier A share is capped rather than allowed to consume
    the whole budget — otherwise the model sees nothing but head terms.
    """
    if df.empty:
        return []

    ranked = df.sort_values("score", ascending=False)

    # Nothing to ration when the whole pool fits: send it all rather than dropping rows
    # that a per-tier quota would have left behind.
    if len(ranked) <= max_rows:
        return ranked.to_dict(orient="records")

    tier_a = ranked[ranked["tier"] == "A"]
    tier_b = ranked[ranked["tier"] == "B"]
    tier_c = ranked[ranked["tier"] == "C"]

    # Long-tail is claimed first, so Tier A can never squeeze it out.
    c_take = min(len(tier_c), max(10, int(max_rows * 0.2)))
    a_take = min(len(tier_a), max_rows - c_take)
    b_take = min(len(tier_b), max_rows - c_take - a_take)

    selected = pd.concat([tier_a.head(a_take), tier_b.head(b_take), tier_c.head(c_take)])

    # A tier may run short of its allocation; top the remaining slots up by score so the
    # planner always receives a full max_rows whenever the pool can supply it.
    if len(selected) < max_rows:
        leftover = ranked[~ranked.index.isin(selected.index)]
        selected = pd.concat([selected, leftover.head(max_rows - len(selected))])

    return selected.head(max_rows).to_dict(orient="records")


def fetch_and_prepare_keywords(
    keyword,
    researcher_type: str = "google_ads",
    geo_zone: Optional[str] = None,
    output_dir: Optional[str] = None
) -> tuple:
    """
    Baseline research over the user's OWN seed keywords — one fetch per seed, merged and
    de-duplicated. No MCP subprocess and no agent-invented queries: the seeds researched are
    exactly the ones the user typed, so a given set of seeds always yields the same pool.

    Accepts a single seed string or a list of seeds.
    Returns: (selected_rows_for_prompt, total_keywords_found)
    """
    load_env_if_present()

    seeds = [keyword] if isinstance(keyword, str) else list(keyword)
    seeds = [str(s).strip() for s in seeds if str(s).strip()]
    if not seeds:
        return [], 0

    fetcher = _fetch_rapidapi_keywords if researcher_type.lower() == "rapidapi" else _fetch_google_ads_keywords

    frames = []
    for seed in seeds:
        try:
            df = fetcher(seed, geo_zone, output_dir)
            if df is not None and not df.empty:
                frames.append(df)
                logger.info(f"Seed '{seed}' returned {len(df)} keywords.")
            else:
                logger.warning(f"Seed '{seed}' returned no keywords.")
        except Exception as seed_err:
            # One bad seed must not lose the results already gathered for the others.
            logger.error(f"Keyword fetch failed for seed '{seed}': {seed_err}")

    if not frames:
        return [], 0

    # Seeds overlap heavily — keep the highest-scoring row per keyword so a term found
    # under two seeds is counted once, with its best metrics.
    merged = pd.concat(frames, ignore_index=True)
    merged = merged.sort_values("score", ascending=False).drop_duplicates(subset=["text"])

    logger.info(f"{len(seeds)} seed(s) researched → {len(merged)} unique keywords.")
    return select_keywords_for_prompt(merged), len(merged)


def fetch_scored_keywords(
    keyword,
    researcher_type: str = "google_ads",
    geo_zone: Optional[str] = None,
    output_dir: Optional[str] = None,
) -> pd.DataFrame:
    """
    Full-pool keyword research — identical fetch-and-merge logic to fetch_and_prepare_keywords
    but returns the COMPLETE de-duplicated DataFrame (not the 50-row LLM-prompt subset).

    Use this when keyword data is NOT being sent to an LLM.  The caller receives every row,
    sorted by score descending, so it can apply its own filtering, tier grouping, or CSV export
    without the prompt-size constraint.

    Accepts a single seed string or a list of seeds.
    Returns: merged pd.DataFrame (columns: text, vol, competition, score, tier)
             — empty DataFrame on error or zero results.
    """
    load_env_if_present()

    seeds = [keyword] if isinstance(keyword, str) else list(keyword)
    seeds = [str(s).strip() for s in seeds if str(s).strip()]
    if not seeds:
        return pd.DataFrame(columns=AGENT_COLUMNS)

    fetcher = _fetch_rapidapi_keywords if researcher_type.lower() == "rapidapi" else _fetch_google_ads_keywords

    frames = []
    for seed in seeds:
        try:
            df = fetcher(seed, geo_zone, output_dir)
            if df is not None and not df.empty:
                frames.append(df)
                logger.info(f"[fetch_scored_keywords] Seed '{seed}' returned {len(df)} keywords.")
            else:
                logger.warning(f"[fetch_scored_keywords] Seed '{seed}' returned no keywords.")
        except Exception as seed_err:
            # One bad seed must not lose results already gathered for the others.
            logger.error(f"[fetch_scored_keywords] Fetch failed for seed '{seed}': {seed_err}")

    if not frames:
        return pd.DataFrame(columns=AGENT_COLUMNS)

    merged = pd.concat(frames, ignore_index=True)
    merged = merged.sort_values("score", ascending=False).drop_duplicates(subset=["text"])

    logger.info(f"[fetch_scored_keywords] {len(seeds)} seed(s) → {len(merged)} unique keywords (full pool).")
    return merged


