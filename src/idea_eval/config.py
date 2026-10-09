from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

Effort = Literal["low", "medium", "high", "xhigh", "max"]


class Settings(BaseSettings):
    """All runtime configuration. Every field can be set from the environment or `.env`."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Credentials
    anthropic_api_key: str = ""
    tavily_api_key: str = ""

    # Models. Workers (brief, analysts) always use the cheap model; the judge and
    # strategist switch to the deep model when a run is started in deep mode.
    cheap_model: str = "claude-haiku-5-5"
    deep_model: str = "claude-sonnet-5-5"
    cheap_effort: Effort = "low"
    deep_effort: Effort = "medium"
    llm_timeout_s: float = 180.0

    # Research caps (the main cost levers)
    max_queries: int = 6
    max_followup_queries: int = 3
    results_per_query: int = 5
    max_sources: int = 14
    max_chars_per_source: int = 2500
    max_research_rounds: int = 2
    search_cache_days: int = 7

    # Web app
    data_dir: Path = Path("./data")
    session_secret: str = ""
    secure_cookies: bool = False
    session_days: int = 14
    default_user_monthly_runs: int = 10
    max_concurrent_runs: int = 2
    monthly_budget_usd: float = 0.0  # 0 disables the global cap

    @property
    def db_path(self) -> Path:
        return self.data_dir / "app.db"


@lru_cache
def get_settings() -> Settings:
    return Settings()
