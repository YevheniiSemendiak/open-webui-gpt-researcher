from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from .domain import ResearchBudget


class Settings(BaseSettings):
    """Runtime configuration shared by API, controller, and runners."""

    model_config = SettingsConfigDict(
        env_prefix="RESEARCH_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    environment: str = "development"
    log_level: str = "INFO"
    database_url: str = "postgresql+asyncpg://research:research@postgres:5432/research"
    auto_create_schema: bool = False
    artifact_base_url: str = "http://localhost:8090"
    internal_base_url: str = "http://api:8090"
    service_token: SecretStr = SecretStr("change-me-service-token")
    signing_secret: SecretStr = SecretStr("change-me-signing-secret-at-least-32-bytes")

    openwebui_url: str = "http://open-webui:8080"
    openwebui_api_key: SecretStr = SecretStr("")

    artifact_backend: Literal["filesystem", "s3"] = "filesystem"
    artifact_path: str = "/data/artifacts"
    s3_endpoint_url: str | None = None
    s3_region: str = "us-east-1"
    s3_bucket: str = "research-artifacts"
    s3_access_key_id: SecretStr = SecretStr("")
    s3_secret_access_key: SecretStr = SecretStr("")

    executor: Literal["local", "kubernetes"] = "local"
    runner_image: str = "ghcr.io/example/open-webui-gpt-researcher:latest"
    runner_namespace: str = "default"
    runner_owner_deployment: str | None = None
    runner_service_account: str = "open-webui-gpt-researcher-runner"
    runner_cpu_request: str = "250m"
    runner_memory_request: str = "512Mi"
    runner_cpu_limit: str = "2"
    runner_memory_limit: str = "2Gi"
    runner_active_deadline_seconds: int = Field(default=7_200, ge=60)
    runner_ttl_seconds_after_finished: int = Field(default=3_600, ge=0)
    runner_extra_env_secret: str | None = None

    controller_poll_seconds: float = Field(default=2.0, ge=0.1)
    controller_leader_election: Literal["database", "none"] = "database"
    controller_leader_lock_id: int = 7_305_809_465_149_768_307
    controller_leader_retry_seconds: float = Field(default=5.0, ge=0.5)
    max_concurrent_jobs: int = Field(default=5, ge=1, le=100)
    engine: Literal["mock", "gpt-researcher"] = "mock"
    public_search_enabled: bool = True
    model_route: Literal["openwebui", "direct"] = "openwebui"
    model_profiles: dict[str, str] = {"default": "gpt-4.1-mini"}
    embedding_model: str = "sentence-transformers/all-MiniLM-L6-v2"
    runner_cancel_poll_seconds: float = Field(default=5.0, ge=0.5)

    default_budget: ResearchBudget = ResearchBudget()
    hard_max_input_tokens: int = 300_000
    hard_max_output_tokens: int = 64_000
    hard_max_searches: int = 100
    hard_max_wall_time_seconds: int = 7_200

    download_token_ttl_seconds: int = Field(default=604_800, ge=60, le=2_592_000)

    def validate_api_secrets(self) -> None:
        if self.environment != "development":
            if self.service_token.get_secret_value().startswith("change-me"):
                msg = "RESEARCH_SERVICE_TOKEN must be configured outside development"
                raise ValueError(msg)
            if self.signing_secret.get_secret_value().startswith("change-me"):
                msg = "RESEARCH_SIGNING_SECRET must be configured outside development"
                raise ValueError(msg)

    def validate_budget(self, budget: ResearchBudget) -> None:
        """Reject user refinements above administrator-defined hard limits."""
        checks = (
            (budget.max_input_tokens, self.hard_max_input_tokens, "max_input_tokens"),
            (budget.max_output_tokens, self.hard_max_output_tokens, "max_output_tokens"),
            (budget.max_searches, self.hard_max_searches, "max_searches"),
            (
                budget.max_wall_time_seconds,
                self.hard_max_wall_time_seconds,
                "max_wall_time_seconds",
            ),
        )
        for value, maximum, name in checks:
            if value > maximum:
                msg = f"{name} exceeds administrator limit {maximum}"
                raise ValueError(msg)

    def resolve_model(self, profile: str) -> str:
        try:
            return self.model_profiles[profile]
        except KeyError as error:
            raise ValueError(f"unknown model profile: {profile}") from error


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
