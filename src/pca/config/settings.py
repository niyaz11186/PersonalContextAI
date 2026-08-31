"""Configuration — loaded and validated once at startup.

Cross-cutting.

Fails fast. A missing GOOGLE_API_KEY surfaces as a boot error with a clear
message rather than as an exception mid-conversation, which matters because the
alternative is losing a user's message to a configuration mistake.

Every model identifier is configuration, never a literal (ADR-002). Model names
move quickly and hardcoding them means a code change to track a provider rename.
"""

from __future__ import annotations

from datetime import timedelta
from functools import lru_cache
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from pca.domain.errors import ConfigurationError


class Settings(BaseSettings):
    """Application configuration.

    Read from environment or a local .env file. Secrets never live in code.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        env_prefix="",
    )

    # --- Gemini. Constraint C-2: Gemini only, no OpenAI anywhere. ---
    google_api_key: str = Field(default="", alias="GOOGLE_API_KEY")
    # Defaults verified against the live API 2026-08-22 (ADR-002). Selected on
    # measured structured-output latency: gemini-3.5-flash resolved a test
    # extraction in 2.9s versus 34s for 3.6-flash and 186s for 3.7-flash.
    llm_model: str = Field(default="gemini-3.5-flash", alias="PCA_LLM_MODEL")
    llm_small_model: str = Field(default="gemini-3.5-flash-lite", alias="PCA_LLM_SMALL_MODEL")
    embedding_model: str = Field(default="gemini-embedding-001", alias="PCA_EMBEDDING_MODEL")
    reranker_model: str = Field(default="gemini-3.5-flash-lite", alias="PCA_RERANKER_MODEL")

    # --- Timezone (ADR-011) ---
    user_timezone: str = Field(default="UTC", alias="PCA_USER_TIMEZONE")

    # --- Stores ---
    postgres_dsn: str = Field(
        default="postgresql://pca:pca@localhost:5432/pca", alias="PCA_POSTGRES_DSN"
    )
    neo4j_uri: str = Field(default="bolt://localhost:7687", alias="PCA_NEO4J_URI")
    neo4j_user: str = Field(default="neo4j", alias="PCA_NEO4J_USER")
    neo4j_password: str = Field(default="", alias="PCA_NEO4J_PASSWORD")

    # --- Behaviour ---
    log_level: str = Field(default="INFO", alias="PCA_LOG_LEVEL")
    retrieval_budget_seconds: int = Field(default=25, alias="PCA_RETRIEVAL_BUDGET_SECONDS")
    extraction_barrier_timeout_seconds: int = Field(
        default=60, alias="PCA_EXTRACTION_BARRIER_TIMEOUT_SECONDS"
    )
    persist_retrieval_diagnostics: bool = Field(
        default=False, alias="PCA_PERSIST_RETRIEVAL_DIAGNOSTICS"
    )

    # --- Bounds (RESILIENCY-10: no unbounded waits, no unbounded concurrency) ---
    #
    # These exist because Unit 5 moves extraction off the request path. Until then
    # every model call was serialised by one user typing, which hid the absence of
    # any limit. `services.md` §Concurrency Model specified the provider semaphore in
    # Inception; it was never built.
    max_concurrent_llm_calls: int = Field(
        default=4, alias="PCA_MAX_CONCURRENT_LLM_CALLS"
    )
    max_concurrent_extractions: int = Field(
        default=2, alias="PCA_MAX_CONCURRENT_EXTRACTIONS"
    )
    # Generous, because structured extraction on the large model measured ~3 s and a
    # cold call can be far slower. The point is a ceiling, not a tight bound.
    llm_timeout_seconds: int = Field(default=120, alias="PCA_LLM_TIMEOUT_SECONDS")
    graph_timeout_seconds: int = Field(default=120, alias="PCA_GRAPH_TIMEOUT_SECONDS")
    # Wall clock for one background extraction, distinct from the barrier timeout.
    # The barrier timeout unblocks the reader; without this the extraction itself
    # runs forever, holding a pool slot and a durable `running` row that
    # recover_pending cannot reclaim because this process still owns it.
    extraction_timeout_seconds: int = Field(
        default=300, alias="PCA_EXTRACTION_TIMEOUT_SECONDS"
    )

    @field_validator("user_timezone")
    @classmethod
    def _validate_timezone(cls, value: str) -> str:
        """Reject an unknown zone at startup.

        Deferring this means the failure appears deep inside temporal resolution,
        where it is far harder to diagnose.
        """
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(
                f"unknown IANA timezone {value!r}; on Windows ensure `tzdata` is installed"
            ) from exc
        return value

    @property
    def retrieval_budget(self) -> timedelta:
        return timedelta(seconds=self.retrieval_budget_seconds)

    @property
    def extraction_barrier_timeout(self) -> timedelta:
        return timedelta(seconds=self.extraction_barrier_timeout_seconds)

    @property
    def extraction_timeout(self) -> timedelta:
        return timedelta(seconds=self.extraction_timeout_seconds)

    def require_runtime_secrets(self) -> None:
        """Assert everything needed to actually run is present.

        Kept separate from construction so that tests and offline tooling can
        build Settings without a live API key, while the real startup path still
        refuses to proceed without one.
        """
        missing: list[str] = []
        if not self.google_api_key:
            missing.append("GOOGLE_API_KEY")
        if not self.neo4j_password:
            missing.append("PCA_NEO4J_PASSWORD")
        if missing:
            raise ConfigurationError(
                "missing required configuration: " + ", ".join(missing)
            )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings, resolved once."""
    return Settings()
