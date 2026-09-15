from __future__ import annotations

import enum
from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, Field, model_validator


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


TERMINAL_STATES = {JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELLED}


class SourceRef(BaseModel):
    """An immutable Open WebUI source selected for a job."""

    kind: Literal["file", "collection"]
    id: str = Field(min_length=1, max_length=255)
    name: str | None = Field(default=None, max_length=512)


class ResearchBudget(BaseModel):
    """User-visible limits validated against administrator caps."""

    max_input_tokens: int = Field(default=120_000, ge=1_000)
    max_output_tokens: int = Field(default=24_000, ge=1_000)
    max_searches: int = Field(default=30, ge=1)
    max_wall_time_seconds: int = Field(default=3_600, ge=60)

    @property
    def max_total_tokens(self) -> int:
        return self.max_input_tokens + self.max_output_tokens


class CreateJobRequest(BaseModel):
    """Request accepted from the trusted Open WebUI adapter."""

    query: str = Field(min_length=3, max_length=50_000)
    chat_id: str = Field(min_length=1, max_length=255)
    message_id: str = Field(min_length=1, max_length=255)
    sources: list[SourceRef] = Field(default_factory=list, max_length=200)
    budget: ResearchBudget = Field(default_factory=ResearchBudget)
    model_profile: str = Field(default="default", min_length=1, max_length=100)
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
        return self


class JobView(BaseModel):
    """Public job representation."""

    id: UUID
    state: JobState
    query: str
    chat_id: str
    message_id: str
    sources: list[SourceRef]
    budget: ResearchBudget
    model_profile: str
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
    budget: ResearchBudget
    model_profile: str
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


class SaveToKnowledgeRequest(BaseModel):
    """Explicit post-run promotion into Open WebUI Knowledge."""

    name: str | None = Field(default=None, min_length=1, max_length=200)
    knowledge_id: str | None = Field(default=None, min_length=1, max_length=255)

    @model_validator(mode="after")
    def choose_create_or_append(self) -> SaveToKnowledgeRequest:
        if bool(self.name) == bool(self.knowledge_id):
            msg = "provide exactly one of name or knowledge_id"
            raise ValueError(msg)
        return self


TokenCount = Annotated[int, Field(ge=0)]


class TokenUsage(BaseModel):
    """Portable token accounting returned by model gateways when available."""

    input_tokens: TokenCount = 0
    output_tokens: TokenCount = 0
