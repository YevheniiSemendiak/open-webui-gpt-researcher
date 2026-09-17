from __future__ import annotations

from functools import lru_cache
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .domain import ModelRoles, ResearchBudget, ResearchShape


class Settings(BaseSettings):
    """Runtime configuration shared by API, controller, and runners."""

    model_config = SettingsConfigDict(
        env_prefix="",
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
    openwebui_timeout_seconds: float = Field(default=600.0, ge=30.0, le=3_600.0)
    function_sources_path: str = "openwebui_functions"

    artifact_backend: Literal["filesystem", "s3"] = "filesystem"
    artifact_path: str = "/data/artifacts"
    s3_endpoint_url: str | None = None
    s3_region: str = "us-east-1"
    s3_bucket: str = "research-artifacts"
    s3_access_key_id: SecretStr = SecretStr("")
    s3_secret_access_key: SecretStr = SecretStr("")

    mode: Literal["local", "k8s"] = "local"
    runner_image: str = "ghcr.io/example/open-webui-gpt-researcher:latest"
    runner_image_pull_policy: Literal["Always", "IfNotPresent", "Never"] = "IfNotPresent"
    runner_image_pull_secrets: list[dict[str, object]] = Field(default_factory=list)
    runner_namespace: str = "default"
    runner_owner_deployment: str | None = None
    runner_service_account: str = "open-webui-gpt-researcher-runner"
    runner_cpu_request: str = "250m"
    runner_memory_request: str = "512Mi"
    runner_cpu_limit: str = "2"
    runner_memory_limit: str = "2Gi"
    runner_active_deadline_seconds: int = Field(default=7_200, ge=60)
    runner_ttl_seconds_after_finished: int = Field(default=3_600, ge=0)
    runner_backoff_limit: int = Field(default=0, ge=0)
    runner_env: dict[str, str | int | float | bool] = Field(default_factory=dict)
    runner_env_from: list[dict[str, object]] = Field(default_factory=list)
    runner_pod_labels: dict[str, str] = Field(default_factory=dict)
    runner_pod_annotations: dict[str, str] = Field(default_factory=dict)
    runner_node_selector: dict[str, str] = Field(default_factory=dict)
    runner_tolerations: list[dict[str, object]] = Field(default_factory=list)
    runner_affinity: dict[str, object] = Field(default_factory=dict)
    runner_pod_security_context: dict[str, object] = Field(
        default_factory=lambda: {
            "runAsNonRoot": True,
            "seccompProfile": {"type": "RuntimeDefault"},
        }
    )
    runner_container_security_context: dict[str, object] = Field(
        default_factory=lambda: {
            "allowPrivilegeEscalation": False,
            "capabilities": {"drop": ["ALL"]},
            "readOnlyRootFilesystem": True,
            "runAsNonRoot": True,
            "runAsUser": 10_001,
        }
    )
    runner_extra_volumes: list[dict[str, object]] = Field(default_factory=list)
    runner_extra_volume_mounts: list[dict[str, object]] = Field(default_factory=list)

    controller_poll_seconds: float = Field(default=2.0, ge=0.1)
    controller_leader_lock_id: int = 7_305_809_465_149_768_307
    controller_leader_retry_seconds: float = Field(default=5.0, ge=0.5)
    dispatch_lease_seconds: int = Field(default=120, ge=10, le=3_600)
    dispatch_reconcile_batch_size: int = Field(default=100, ge=1, le=10_000)
    runner_heartbeat_seconds: float = Field(default=10.0, ge=1.0, le=300.0)
    runner_stale_seconds: int = Field(default=60, ge=10, le=3_600)
    runner_max_attempts: int = Field(default=2, ge=1, le=10)
    max_concurrent_jobs: int = Field(default=5, ge=1, le=100)
    public_search_enabled: bool = True
    retriever: str = "searx"
    scraper: str = "nodriver"
    searx_url: str = "http://searxng:8080"
    crawler_proxy_url: str | None = None
    model_route: Literal["openwebui", "direct"] = "openwebui"
    default_model_profiles: dict[str, str | ModelRoles] = {"default": "gpt-4.1-mini"}
    default_research_strategy: Literal["focused", "balanced", "broad", "deep"] = "balanced"
    model_context_safety_tokens: int = Field(default=256, ge=0, le=8_192)
    reasoning_effort: Literal["low", "medium", "high"] | None = None
    embedding_model: str = "sentence-transformers/all-MiniLM-L6-v2"
    runner_cancel_poll_seconds: float = Field(default=1.0, ge=0.5)

    default_budget: ResearchBudget = ResearchBudget()
    hard_max_input_tokens: int = Field(default=300_000, ge=1)
    hard_max_output_tokens: int = Field(default=64_000, ge=1)  # 64_000
    hard_max_queries: int = Field(default=100, ge=1)
    hard_max_wall_time_seconds: int = 7_200

    download_token_ttl_seconds: int = Field(default=604_800, ge=60, le=2_592_000)

    event_retention_days: int = Field(default=30, ge=1, le=3_650)
    artifact_retention_days: int = Field(default=90, ge=1, le=3_650)
    job_retention_days: int = Field(default=90, ge=1, le=3_650)
    orphan_grace_seconds: int = Field(default=86_400, ge=3_600, le=2_592_000)
    cleanup_batch_size: int = Field(default=100, ge=1, le=10_000)

    @field_validator("crawler_proxy_url", mode="before")
    @classmethod
    def validate_crawler_proxy_url(cls, value: object) -> object:
        if value in (None, ""):
            return None
        if not isinstance(value, str):
            return value
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https", "socks4", "socks5"} or not parsed.hostname:
            msg = "CRAWLER_PROXY_URL must be an HTTP, HTTPS, SOCKS4, or SOCKS5 URL"
            raise ValueError(msg)
        if parsed.username is not None or parsed.password is not None:
            msg = "CRAWLER_PROXY_URL must not embed credentials"
            raise ValueError(msg)
        return value

    @model_validator(mode="after")
    def validate_retention_order(self) -> Settings:
        if self.job_retention_days < self.artifact_retention_days:
            msg = "JOB_RETENTION_DAYS must be greater than or equal to ARTIFACT_RETENTION_DAYS"
            raise ValueError(msg)
        if self.runner_stale_seconds <= self.runner_heartbeat_seconds * 2:
            msg = "RUNNER_STALE_SECONDS must exceed twice RUNNER_HEARTBEAT_SECONDS"
            raise ValueError(msg)
        reserved_env = {"JOB_ID", "RUNNER_TOKEN", "INTERNAL_BASE_URL"}
        if conflict := reserved_env.intersection(self.runner_env):
            msg = f"RUNNER_ENV cannot override reserved values: {', '.join(sorted(conflict))}"
            raise ValueError(msg)
        if any(volume.get("name") == "tmp" for volume in self.runner_extra_volumes):
            raise ValueError("RUNNER_EXTRA_VOLUMES cannot redefine the tmp volume")
        if any(
            mount.get("name") == "tmp" or mount.get("mountPath") == "/tmp"  # noqa: S108
            for mount in self.runner_extra_volume_mounts
        ):
            raise ValueError("RUNNER_EXTRA_VOLUME_MOUNTS cannot redefine the tmp mount")
        return self

    def validate_api_secrets(self) -> None:
        if self.environment != "development":
            if self.service_token.get_secret_value().startswith("change-me"):
                msg = "SERVICE_TOKEN must be configured outside development"
                raise ValueError(msg)
            if self.signing_secret.get_secret_value().startswith("change-me"):
                msg = "SIGNING_SECRET must be configured outside development"
                raise ValueError(msg)

    def validate_budget(self, budget: ResearchBudget) -> None:
        """Reject user refinements above administrator-defined hard limits."""
        checks = (
            (budget.max_input_tokens, self.hard_max_input_tokens, "max_input_tokens"),
            (budget.max_output_tokens, self.hard_max_output_tokens, "max_output_tokens"),
            (budget.max_queries, self.hard_max_queries, "max_queries"),
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

    def validate_research_shape(self, research: ResearchShape, budget: ResearchBudget) -> None:
        """Ensure the requested tree can finish within the query budget."""
        required = research.estimated_max_queries
        if required > budget.max_queries:
            msg = (
                f"{research.strategy} research may require up to {required} queries, "
                f"but max_queries is {budget.max_queries}"
            )
            raise ValueError(msg)

    def resolve_default_models(self, profile: str = "default") -> ModelRoles:
        try:
            configured = self.default_model_profiles[profile]
        except KeyError as error:
            raise ValueError(f"unknown default model profile: {profile}") from error
        if isinstance(configured, str):
            return ModelRoles(
                fast=configured,
                smart=configured,
                strategic=configured,
            )
        return configured


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
