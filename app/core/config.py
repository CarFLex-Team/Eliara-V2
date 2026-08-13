"""Application settings.

Single source of truth for configuration. Everything is environment-driven
(12-factor); no other module reads os.environ directly.
"""

from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="ELIARA_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Runtime ---
    environment: str = Field(default="dev", description="dev | staging | prod")
    log_level: str = "INFO"

    # --- Multi-company registry ---
    # Single source of truth for which companies exist and where each
    # one's database/prompts/playbooks/scan-views live. See
    # app/company/registry.py. Platform-wide Settings should not hold
    # company-specific values (db path, scan views, ...) going forward —
    # those move into CompanyConfig entries in this file.
    companies_config_path: Path = Field(
        default=Path("companies.yaml"), validation_alias="ELIARA_COMPANIES_CONFIG"
    )
    # Company used by legacy root-mounted endpoints (POST /ask) that don't
    # send a company_id, so the existing deployed frontend keeps working
    # unmodified. New clients should send company_id explicitly.
    default_company_id: str = "beta"

    # --- Database ---
    # NOTE: db_path/scan_views below are legacy, pre-multi-company fields.
    # They remain only as fallbacks for any test/tool that constructs a
    # single ReadOnlyExecutor/Orchestrator directly without going through
    # CompanyContextManager. Company-scoped requests always resolve their
    # database path and scan views from CompanyConfig (companies.yaml),
    # never from here.
    db_path: Path = Field(default=Path("data/eliara_production_clean.db"))
    db_watch_interval_s: int = 60
    query_timeout_s: int = 30
    max_rows: int = 500

    # --- LLM ---
    anthropic_api_key: SecretStr = SecretStr("")
    orchestrator_model: str = "claude-sonnet-4-6"
    sqlgen_model: str = "claude-haiku-4-5-20251001"
    llm_timeout_s: int = 40      # per LLM call
    llm_max_retries: int = 2
    # Whole-request ceiling. Must stay UNDER the Cloudflare edge timeout
    # (100s), or the tunnel returns 524 and the browser shows a generic
    # error bubble with no explanation. 2 LLM calls x 40s x 3 attempts
    # could otherwise reach ~4 minutes.
    request_deadline_s: int = 75

    # --- Discovery ---
    embedding_backend: str = Field(default="bge", description="bge | hashing | none")
    embedding_model_name: str = "BAAI/bge-base-en-v1.5"
    embedding_cache_dir: Path = Field(default=Path("data/cache"))
    top_k_views: int = 8
    # Scanning a large fact table for entity values blocks startup before
    # the port binds. Dimensions only unless you have measured the cost.
    entity_index_include_facts: bool = False
    # Explicit sources when auto-detection misses one, e.g.
    #   ELIARA_ENTITY_INDEX_SOURCES="customer=fact_ai_sales_net"
    entity_index_sources: str = ""

    # --- Conversation ---
    history_size: int = 5
    session_ttl_min: int = 120

    # --- API ---
    max_message_chars: int = 2000
    chat_rate_limit_per_min: int = 20
    cors_origins: str = "*"          # comma-separated origins, or "*"
    legacy_api_enabled: bool = True  # mount POST /ask + GET /health at root
    legacy_api_key: str = ""         # optional X-API-Key guard for /v1/query
    # Shared secret for POST /ask. EMPTY = open, which is what the live
    # browser-direct frontend needs today. Set it only once the frontend
    # proxies through its own server (see AUTH.md) — setting it while the
    # browser still calls /ask directly will break the site.
    ask_shared_secret: str = ""

    # --- Audit trail ---
    audit_enabled: bool = True
    audit_dir: Path = Field(default=Path("audit"))

    # --- Playbooks ---
    playbooks_enabled: bool = True

    # --- Answer shape ---
    # Management reads on a phone. 7 of 18 audited answers exceeded 1,500
    # characters; this is the ceiling handed to the answer prompt.
    answer_char_budget: int = 1200
    answer_max_bullets: int = 5
    # Verification is advisory by default: the report is logged and
    # attached to the response. Strict mode appends a visible caveat when
    # a figure cannot be traced to the result set.
    verification_strict: bool = False

    # --- Result cache / payload budget ---
    result_cache_ttl_s: int = 600
    payload_max_chars: int = 6000

    # --- Reasoning agent (prototype; off by default) ---
    # The loop is bounded three ways and stops at whichever binds first,
    # always producing an answer from what it gathered. Steps are the budget
    # that matters: at ~26ms per output token, each extra step is a visible
    # second on the wall clock, so this stays small until streaming lands.
    agent_enabled: bool = False
    agent_max_steps: int = 4
    agent_time_budget_s: int = 55       # must stay under request_deadline_s
    agent_token_budget: int = 60_000    # cumulative input tokens per turn

    # --- External knowledge (/search prefix; off by default) ---
    # A question the DATABASE cannot answer — general knowledge, current
    # events, supplier landscape. Reached ONLY by an explicit user prefix,
    # never by the router: every routing misjudgement this session came from
    # the model guessing intent, and a prefix is the user stating it. That
    # also means no retrieval and no routing LLM call at all on this path.
    external_enabled: bool = False
    external_prefix: str = "/search"
    # Live web search via the Anthropic tool. With this OFF the model answers
    # from training data alone, so the prompt must refuse anything
    # time-sensitive — an answer about "today's" weather from a stale model
    # is exactly the silent-confident-wrongness this platform tries to avoid.
    external_web_search: bool = True
    external_max_searches: int = 5

    # --- Explicit investigate access (bypasses routing entirely) ---
    # "why did margin drop" failed to reach the investigate decision THREE
    # separate times through the router in production despite the routing
    # prompt naming that exact phrase as its canonical trigger example — the
    # router's judgment call is simply not reliable enough for this to be the
    # only way in. A prefix sidesteps the judgment call the same way /search
    # does. Tied to agent_enabled rather than a separate flag: if the loop
    # itself is off, the prefix degrades to a clear message rather than a
    # dead end.
    investigate_prefix: str = "/investigate"

    # --- Proactive attention scan (bypasses routing, no view name required) ---
    # A business owner should never need to know a curated view's internal
    # name. This scans a pre-registered list of views (the same ones
    # app/orchestrator/definitions/stock_action_plan.yaml already runs) and
    # narrates the HIGH-tier findings in plain English — the deterministic
    # ranking from app/detection/attention_queue.py, reachable without ever
    # typing a SQL object name.
    scan_enabled: bool = False
    scan_prefix: str = "/scan"
    # LEGACY fallback only — company-scoped Orchestrators receive their
    # scan_views from CompanyConfig (companies.yaml) instead. This list is
    # used only when an Orchestrator is constructed directly without a
    # company_id/scan_views override (e.g. an old test).
    scan_views: list[str] = [
        "vw_q012_liquidation_items_highest_capital_locked",
        "vw_engine_dead_stock_by_item_group",
        "vw_engine_dead_stock_by_warehouse",
        "vw_chatbot_slow_moving_items_becoming_dead_stock",
        "vw_q018_slow_moving_items_review_priority",
        "vw_inventory_action_stockout_replenishment",
        "vw_inventory_action_overstock_excess",
    ]
    scan_max_items_per_view: int = 5

    @property
    def is_prod(self) -> bool:
        return self.environment == "prod"


@lru_cache
def get_settings() -> Settings:
    return Settings()
