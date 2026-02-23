"""
Configuration settings using pydantic-settings.
"""
from pydantic_settings import BaseSettings
from pydantic import Field, field_validator, ValidationError


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    # OpenRouter API Configuration
    openrouter_api_key: str
    openrouter_model: str = "arcee-ai/trinity-large-preview:free"
    openrouter_base_url: str = "https://openrouter.ai/api/v1"

    # Crawl4AI Configuration
    max_concurrent_crawls: int = Field(default=5, ge=1, le=50)
    page_timeout: int = Field(default=30000, ge=5000, le=300000)  # 5s to 5 minutes

    # Phone Number Configuration
    default_country: str = Field(default="AU", pattern=r"^(AU|NZ|UK|US)$")

    # LLM Configuration
    max_decision_makers: int = Field(default=3, ge=1, le=10)
    llm_temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    reasoning_enabled: bool = False  # MiMo-V2-Flash reasoning mode

    # Confidence Thresholds
    min_confidence_threshold: float = Field(default=0.5, ge=0.0, le=1.0)

    # Output Configuration
    output_dir: str = "data/output"

    # Retry Configuration
    max_retries: int = Field(default=3, ge=0, le=10)
    retry_delay: float = Field(default=2.0, ge=0.0, le=60.0)  # Seconds

    # Rate Limiting
    delay_between_requests: float = Field(default=1.0, ge=0.0, le=60.0)  # Seconds

    @field_validator("openrouter_api_key")
    @classmethod
    def validate_api_key(cls, v: str) -> str:
        """
        Validate OpenRouter API key format.

        OpenRouter keys typically start with "sk-or-" not just "sk-".
        Ensure the key is not empty and has reasonable length.
        """
        if not v:
            raise ValueError("OPENROUTER_API_KEY must be provided")
        if len(v) < 20:
            raise ValueError("OPENROUTER_API_KEY appears to be invalid (too short)")
        return v

    @field_validator("default_country")
    @classmethod
    def validate_default_country(cls, v: str) -> str:
        """Validate default country code."""
        v = v.upper()
        if v not in ["AU", "NZ", "UK", "US"]:
            raise ValueError(f"default_country must be one of: AU, NZ, UK, US (got: {v})")
        return v

    class Config:
        env_file = ".env"
        case_sensitive = False
