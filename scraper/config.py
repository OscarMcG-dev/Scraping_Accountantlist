"""Configuration settings using pydantic-settings."""
from pydantic_settings import BaseSettings
from pydantic import Field


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    # OpenRouter API (Phase 2)
    openrouter_api_key: str = ""
    openrouter_model: str = "x-ai/grok-4.1-fast"
    openrouter_base_url: str = "https://openrouter.ai/api/v1"

    # Attio API (Phase 3 dedup)
    attio_api_key: str = ""

    # Jina AI API (optional, for reader/search fallback)
    jina_api_key: str = ""

    # Directory scraper (Phase 1)
    directory_delay: float = Field(default=1.0, ge=0.0, le=30.0)
    directory_max_concurrent: int = Field(default=5, ge=1, le=20)

    # Crawl4AI (Phase 2)
    max_concurrent_crawls: int = Field(default=5, ge=1, le=50)
    page_timeout: int = Field(default=45000, ge=5000, le=300000)
    # Max sub-pages to crawl per site (main page is separate). We crawl *up to* this many:
    # the actual count is decided by the LLM (up to N picks) or keyword/safe fallback.
    max_crawl_subpages: int = Field(default=10, ge=1, le=20)

    # LLM
    max_decision_makers: int = Field(default=3, ge=1, le=10)
    llm_temperature: float = Field(default=0.0, ge=0.0, le=2.0)

    # Web search (Phase 2b) — fires when crawl finds no decision makers
    web_search_enabled: bool = True
    web_search_max_results: int = Field(default=3, ge=1, le=10)
    web_search_model: str = "x-ai/grok-4.1-fast:online"

    # LLM link triage — use LLM to pick which sub-pages to crawl
    llm_link_triage: bool = True

    # Output
    output_dir: str = "data/output"

    # Retry
    max_retries: int = Field(default=3, ge=0, le=10)
    retry_delay: float = Field(default=2.0, ge=0.0, le=60.0)

    # JustCall API (campaign creation from dashboard)
    justcall_api_key: str = ""
    justcall_api_secret: str = ""
    justcall_base_url: str = "https://api.justcall.io/v2.1"

    model_config = {"env_file": ".env", "case_sensitive": False}
