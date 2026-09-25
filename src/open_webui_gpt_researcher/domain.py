from __future__ import annotations

import enum
from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


def default_report_formats() -> list[Literal["markdown", "json"]]:
    return ["markdown", "json"]


class JobState(enum.StrEnum):
    """Durable research-job lifecycle."""

    PENDING = "pending"
    DISPATCHED = "dispatched"
    RUNNING = "running"
    CANCEL_REQUESTED = "cancel_requested"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ResearchStage(enum.StrEnum):
    """Stable research lifecycle stages shared by progress events and metrics."""

    STARTING = "starting"
    RETRIEVAL = "retrieval"
    PLANNING = "planning"
    RESEARCHING = "researching"
    WRITING = "writing"
    FINALIZING = "finalizing"


TERMINAL_STATES = {JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELLED}


class SourceRef(BaseModel):
    """An immutable Open WebUI source selected for a job."""

    kind: Literal["file", "collection"]
    id: str = Field(min_length=1, max_length=255)
    name: str | None = Field(default=None, max_length=512)


class ContextDocument(BaseModel):
    """User-authorized Open WebUI conversation context frozen for one run."""

    kind: Literal["conversation", "linked_chat"]
    id: str = Field(min_length=1, max_length=255)
    title: str | None = Field(default=None, max_length=1_024)
    text: str = Field(min_length=1, max_length=300_000)
    chat_id: str = Field(min_length=1, max_length=255)


class ModelRoles(BaseModel):
    """Open WebUI model IDs selected for one research run."""

    fast: str = Field(min_length=1, max_length=512)
    smart: str = Field(min_length=1, max_length=512)
    strategic: str = Field(min_length=1, max_length=512)


class ModelCapability(BaseModel):
    """Frozen limits published by Open WebUI for an accessible model."""

    id: str = Field(min_length=1, max_length=512)
    context_length: int = Field(ge=4_096)
    max_output_tokens: int = Field(ge=1)


class ModelLimits(BaseModel):
    """Administrator-supplied fallback limits for one Open WebUI model."""

    model_config = ConfigDict(extra="forbid")

    context_length: int = Field(ge=4_096)
    max_output_tokens: int = Field(ge=1)

    @model_validator(mode="after")
    def validate_output_capacity(self) -> ModelLimits:
        if self.max_output_tokens > self.context_length:
            raise ValueError("max_output_tokens must not exceed context_length")
        return self


ResearchStrategy = Literal["focused", "balanced", "broad", "deep", "custom"]

RESEARCH_PRESETS: dict[str, tuple[int, int, int]] = {
    "focused": (1, 1, 2),
    "balanced": (2, 2, 2),
    "broad": (4, 2, 2),
    "deep": (2, 3, 3),
}


class ResearchShape(BaseModel):
    """Frozen research-tree shape selected for one run."""

    strategy: ResearchStrategy = "balanced"
    breadth: int = Field(default=2, ge=1, le=8)
    depth: int = Field(default=2, ge=1, le=4)
    queries_per_branch: int = Field(default=2, ge=1, le=5)

    @model_validator(mode="after")
    def validate_preset(self) -> ResearchShape:
        preset = RESEARCH_PRESETS.get(self.strategy)
        selected = (self.breadth, self.depth, self.queries_per_branch)
        if preset is not None and selected != preset:
            msg = (
                f"{self.strategy} research must use breadth={preset[0]}, "
                f"depth={preset[1]}, and queries_per_branch={preset[2]}"
            )
            raise ValueError(msg)
        return self

    @property
    def total_workers(self) -> int:
        current_breadth = self.breadth
        workers_at_level = self.breadth
        workers = 0
        for level in range(self.depth):
            if level:
                current_breadth = max(2, current_breadth // 2)
                workers_at_level *= current_breadth
            workers += workers_at_level
        return workers

    @property
    def estimated_max_queries(self) -> int:
        # Each nested researcher performs one planning query, the generated
        # queries, and its original query. The root planner adds one query.
        return 1 + self.total_workers * (self.queries_per_branch + 2)


class ResearchBudget(BaseModel):
    """User-visible operational limits validated against administrator caps."""

    model_config = ConfigDict(extra="forbid")

    max_queries: int = Field(default=100, ge=1)
    max_wall_time_seconds: int = Field(default=3_600, ge=60)


class CreateJobRequest(BaseModel):
    """Request accepted from the trusted Open WebUI adapter."""

    query: str = Field(min_length=3, max_length=50_000)
    chat_id: str = Field(min_length=1, max_length=255)
    message_id: str = Field(min_length=1, max_length=255)
    sources: list[SourceRef] = Field(default_factory=list, max_length=200)
    context_documents: list[ContextDocument] = Field(default_factory=list, max_length=50)
    research: ResearchShape = Field(default_factory=ResearchShape)
    budget: ResearchBudget = Field(default_factory=ResearchBudget)
    models: ModelRoles
    model_capabilities: list[ModelCapability] = Field(min_length=1, max_length=3)
    report_type: Literal["research_report", "deep", "detailed_report"] = "deep"
    report_formats: list[Literal["markdown", "json"]] = Field(
        default_factory=default_report_formats,
        min_length=1,
    )

    @model_validator(mode="after")
    def unique_sources(self) -> CreateJobRequest:
        identities = [(source.kind, source.id) for source in self.sources]
        if len(identities) != len(set(identities)):
            msg = "sources must be unique"
            raise ValueError(msg)
        context_identities = [(document.kind, document.id) for document in self.context_documents]
        if len(context_identities) != len(set(context_identities)):
            msg = "context documents must be unique"
            raise ValueError(msg)
        if sum(len(document.text) for document in self.context_documents) > 500_000:
            msg = "context documents exceed the 500000 character request limit"
            raise ValueError(msg)
        capability_ids = [capability.id for capability in self.model_capabilities]
        if len(capability_ids) != len(set(capability_ids)):
            msg = "model capabilities must be unique"
            raise ValueError(msg)
        selected = {self.models.fast, self.models.smart, self.models.strategic}
        if selected != set(capability_ids):
            msg = "every selected model must have exactly one capability snapshot"
            raise ValueError(msg)
        return self


class JobView(BaseModel):
    """Public job representation."""

    id: UUID
    state: JobState
    query: str
    chat_id: str
    message_id: str
    sources: list[SourceRef]
    parent_job_id: UUID | None = None
    iteration: int = 1
    context_document_count: int = 0
    research: ResearchShape = Field(default_factory=ResearchShape)
    budget: ResearchBudget
    models: ModelRoles
    model_capabilities: list[ModelCapability]
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    result: dict[str, object] | None = None
    usage: dict[str, object] | None = None
    error: str | None = None


class JobEventView(BaseModel):
    """A sequenced event suitable for polling or SSE."""

    sequence: int
    event_type: str
    data: dict[str, object]
    created_at: datetime


class RunnerJobSpec(BaseModel):
    """Frozen job specification exposed only to its runner."""

    id: UUID
    query: str
    sources: list[SourceRef]
    context_documents: list[ContextDocument] = Field(default_factory=list)
    parent_job_id: UUID | None = None
    iteration: int = 1
    research: ResearchShape = Field(default_factory=ResearchShape)
    budget: ResearchBudget
    remaining_wall_time_seconds: int | None = Field(default=None, ge=1)
    models: ModelRoles
    model_capabilities: list[ModelCapability]
    report_type: str
    report_formats: list[str]


class RunnerEvent(BaseModel):
    """Progress produced by a runner."""

    event_type: str = Field(min_length=1, max_length=100)
    data: dict[str, object] = Field(default_factory=dict)


class RunnerCompletion(BaseModel):
    """Research results returned by a runner."""

    report_markdown: str
    research_notes_markdown: str = ""
    sources: list[dict[str, object]] = Field(default_factory=list)
    usage: dict[str, object] = Field(default_factory=dict)


TokenCount = Annotated[int, Field(ge=0)]


class TokenUsage(BaseModel):
    """Portable token accounting returned by model gateways when available."""

    input_tokens: TokenCount = 0
    output_tokens: TokenCount = 0
