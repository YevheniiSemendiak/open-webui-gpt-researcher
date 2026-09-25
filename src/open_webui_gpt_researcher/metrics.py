from __future__ import annotations

import asyncio
import math
import time
from collections import defaultdict
from collections.abc import Awaitable, Callable
from contextlib import suppress
from datetime import UTC, datetime, timedelta

from fastapi import FastAPI, Request, Response
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)
from sqlalchemy import case, func, select

from .db import Database, ResearchArtifact, ResearchJob
from .domain import JobState, ResearchStage

STATE_REFRESH_SECONDS = 15
RECENT_WINDOWS = {"1h": timedelta(hours=1), "24h": timedelta(hours=24)}
QUANTILES = (0.5, 0.95, 0.99)
STRATEGIES = ("focused", "balanced", "broad", "deep", "custom", "unknown")
USAGE_KINDS = (
    "input_tokens",
    "output_tokens",
    "searches",
    "private_searches",
    "estimated_token_calls",
)


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _seconds_between(later: datetime, earlier: datetime) -> float:
    return max(0.0, (_as_utc(later) - _as_utc(earlier)).total_seconds())


def _quantile(values: list[float], quantile: float) -> float:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * quantile) - 1)]


class ResearchMetrics:
    """Prometheus instruments for the durable research control plane."""

    def __init__(self) -> None:
        self.registry = CollectorRegistry(auto_describe=True)
        self.http_requests = Counter(
            "deep_research_http_requests_total",
            "HTTP requests received by the researcher API.",
            ("method", "route", "status"),
            registry=self.registry,
        )
        self.http_duration = Histogram(
            "deep_research_http_request_duration_seconds",
            "Researcher API request duration.",
            ("method", "route"),
            registry=self.registry,
        )
        self.jobs_retained = Gauge(
            "deep_research_jobs_retained",
            "Research jobs retained in PostgreSQL by current state and strategy.",
            ("state", "strategy"),
            registry=self.registry,
        )
        self.jobs_completed_recent = Gauge(
            "deep_research_jobs_completed_recent",
            "Terminal research jobs completed within a rolling database-derived window.",
            ("state", "strategy", "window"),
            registry=self.registry,
        )
        self.jobs_current = Gauge(
            "deep_research_jobs_current",
            "Durable research jobs by current state.",
            ("state",),
            registry=self.registry,
        )
        self.oldest_pending_age = Gauge(
            "deep_research_oldest_pending_age_seconds",
            "Age of the oldest pending research job, or zero when none are pending.",
            registry=self.registry,
        )
        self.queue_wait = Gauge(
            "deep_research_job_queue_wait_seconds",
            "Database-derived quantiles for time from job creation until runner start.",
            ("strategy", "quantile"),
            registry=self.registry,
        )
        self.execution_duration = Gauge(
            "deep_research_job_execution_duration_seconds",
            "Database-derived execution-duration quantiles for terminal jobs.",
            ("state", "strategy", "quantile"),
            registry=self.registry,
        )
        self.end_to_end_duration = Gauge(
            "deep_research_job_end_to_end_duration_seconds",
            "Database-derived end-to-end duration quantiles for terminal jobs.",
            ("state", "strategy", "quantile"),
            registry=self.registry,
        )
        self.dispatches = Counter(
            "deep_research_dispatch_total",
            "Runner dispatch attempts by result.",
            ("result",),
            registry=self.registry,
        )
        self.reconciliations = Counter(
            "deep_research_reconciliation_total",
            "Controller reconciliations by kind and action.",
            ("kind", "action"),
            registry=self.registry,
        )
        self.controller_leader = Gauge(
            "deep_research_controller_leader",
            "Whether this API replica currently owns controller leadership.",
            registry=self.registry,
        )
        self.progress_events = Counter(
            "deep_research_progress_events_total",
            "Runner progress events by bounded research stage.",
            ("stage",),
            registry=self.registry,
        )
        self.searches = Counter(
            "deep_research_search_requests_total",
            "Search requests made through the control plane.",
            ("source", "result"),
            registry=self.registry,
        )
        self.search_duration = Histogram(
            "deep_research_search_duration_seconds",
            "Search request duration.",
            ("source",),
            registry=self.registry,
        )
        self.llm_requests = Counter(
            "deep_research_llm_requests_total",
            "LLM and embedding requests by role and result.",
            ("kind", "role", "result"),
            registry=self.registry,
        )
        self.llm_duration = Histogram(
            "deep_research_llm_request_duration_seconds",
            "LLM and embedding request duration.",
            ("kind", "role"),
            buckets=(0.1, 0.5, 1, 2.5, 5, 10, 30, 60, 120, 300, 600),
            registry=self.registry,
        )
        self.usage = Gauge(
            "deep_research_usage_retained_total",
            "Cumulative accounted usage across research jobs retained in PostgreSQL.",
            ("kind",),
            registry=self.registry,
        )
        self.artifacts_current = Gauge(
            "deep_research_artifacts_current",
            "Research artifacts currently recorded in PostgreSQL.",
            registry=self.registry,
        )
        self.artifact_bytes_current = Gauge(
            "deep_research_artifact_bytes_current",
            "Research artifact bytes currently recorded in PostgreSQL.",
            registry=self.registry,
        )
        self.artifact_write_failures = Counter(
            "deep_research_artifact_write_failures_total",
            "Artifact writes that failed before being recorded in PostgreSQL.",
            registry=self.registry,
        )

    def install(self, app: FastAPI) -> None:
        @app.get("/metrics", include_in_schema=False)
        async def prometheus_metrics() -> Response:
            return Response(
                generate_latest(self.registry), headers={"Content-Type": CONTENT_TYPE_LATEST}
            )

        @app.middleware("http")
        async def observe_http(
            request: Request, call_next: Callable[[Request], Awaitable[Response]]
        ) -> Response:
            started = time.perf_counter()
            status = 500
            try:
                response = await call_next(request)
                status = response.status_code
                return response
            finally:
                route = request.scope.get("route")
                route_path = getattr(route, "path", "unmatched")
                if route_path != "/metrics":
                    self.http_requests.labels(request.method, route_path, str(status)).inc()
                    self.http_duration.labels(request.method, route_path).observe(
                        time.perf_counter() - started
                    )

    def observe_progress(self, data: dict[str, object]) -> None:
        try:
            stage = ResearchStage(str(data.get("stage", ResearchStage.RESEARCHING.value))).value
        except ValueError:
            stage = "other"
        self.progress_events.labels(stage).inc()

    async def refresh_durable_state(self, database: Database) -> None:
        now = datetime.now(UTC)
        async with database.session() as session:
            raw_strategy = ResearchJob.research["strategy"].as_string()
            strategy_column = case(
                (
                    raw_strategy.in_(STRATEGIES[:-1]),
                    raw_strategy,
                ),
                else_="unknown",
            )
            state_rows = (
                await session.execute(
                    select(ResearchJob.state, func.count()).group_by(ResearchJob.state)
                )
            ).all()
            retained_rows = (
                await session.execute(
                    select(ResearchJob.state, strategy_column, func.count()).group_by(
                        ResearchJob.state, strategy_column
                    )
                )
            ).all()
            recent_rows: list[tuple[str, str, str, int]] = []
            for window, duration in RECENT_WINDOWS.items():
                aggregates = (
                    await session.execute(
                        select(ResearchJob.state, strategy_column, func.count())
                        .where(
                            ResearchJob.state.in_(
                                (
                                    JobState.SUCCEEDED.value,
                                    JobState.FAILED.value,
                                    JobState.CANCELLED.value,
                                )
                            ),
                            ResearchJob.finished_at >= now - duration,
                        )
                        .group_by(ResearchJob.state, strategy_column)
                    )
                ).all()
                recent_rows.extend(
                    (str(state), str(strategy), window, int(count))
                    for state, strategy, count in aggregates
                )
            rows = (
                await session.execute(
                    select(
                        ResearchJob.state,
                        ResearchJob.research,
                        ResearchJob.usage,
                        ResearchJob.created_at,
                        ResearchJob.started_at,
                        ResearchJob.finished_at,
                    )
                )
            ).all()
            oldest_pending = await session.scalar(
                select(func.min(ResearchJob.created_at)).where(
                    ResearchJob.state == JobState.PENDING.value
                )
            )
            artifact_count, artifact_bytes = (
                await session.execute(
                    select(
                        func.count(ResearchArtifact.id),
                        func.coalesce(func.sum(ResearchArtifact.size), 0),
                    )
                )
            ).one()

        current = {str(state): int(count) for state, count in state_rows}
        retained = {
            (str(state), str(strategy)): int(count) for state, strategy, count in retained_rows
        }
        completed_recent = {
            (state, strategy, window): count for state, strategy, window, count in recent_rows
        }
        queue_waits: dict[str, list[float]] = defaultdict(list)
        execution_durations: dict[tuple[str, str], list[float]] = defaultdict(list)
        end_to_end_durations: dict[tuple[str, str], list[float]] = defaultdict(list)
        usage_totals = dict.fromkeys(USAGE_KINDS, 0)
        terminal_states = {
            JobState.SUCCEEDED.value,
            JobState.FAILED.value,
            JobState.CANCELLED.value,
        }
        for state, research, usage, created_at, started_at, finished_at in rows:
            state = str(state)
            strategy_value = (research or {}).get("strategy", "unknown")
            strategy = str(strategy_value) if strategy_value in STRATEGIES[:-1] else "unknown"
            if started_at is not None:
                queue_waits[strategy].append(_seconds_between(started_at, created_at))
            if state in terminal_states and finished_at is not None:
                end_to_end_durations[state, strategy].append(
                    _seconds_between(finished_at, created_at)
                )
                if started_at is not None:
                    execution_durations[state, strategy].append(
                        _seconds_between(finished_at, started_at)
                    )
            for kind in USAGE_KINDS:
                usage_totals[kind] += max(0, int((usage or {}).get(kind, 0)))

        for state in JobState:
            self.jobs_current.labels(state.value).set(current.get(state.value, 0))

        self.jobs_retained.clear()
        for state in JobState:
            for strategy in STRATEGIES:
                self.jobs_retained.labels(state.value, strategy).set(0)
        for (state, strategy), count in retained.items():
            self.jobs_retained.labels(state, strategy).set(count)

        self.jobs_completed_recent.clear()
        for state in (JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELLED):
            for strategy in STRATEGIES:
                for window in RECENT_WINDOWS:
                    self.jobs_completed_recent.labels(state.value, strategy, window).set(0)
        for (state, strategy, window), count in completed_recent.items():
            self.jobs_completed_recent.labels(state, strategy, window).set(count)

        self.queue_wait.clear()
        for strategy, values in queue_waits.items():
            for quantile in QUANTILES:
                self.queue_wait.labels(strategy, str(quantile)).set(_quantile(values, quantile))

        self.execution_duration.clear()
        for (state, strategy), values in execution_durations.items():
            for quantile in QUANTILES:
                self.execution_duration.labels(state, strategy, str(quantile)).set(
                    _quantile(values, quantile)
                )

        self.end_to_end_duration.clear()
        for (state, strategy), values in end_to_end_durations.items():
            for quantile in QUANTILES:
                self.end_to_end_duration.labels(state, strategy, str(quantile)).set(
                    _quantile(values, quantile)
                )

        for kind, value in usage_totals.items():
            self.usage.labels(kind).set(value)
        self.artifacts_current.set(int(artifact_count))
        self.artifact_bytes_current.set(int(artifact_bytes))
        age = 0.0 if oldest_pending is None else _seconds_between(now, oldest_pending)
        self.oldest_pending_age.set(age)

    async def refresh_forever(self, database: Database) -> None:
        while True:
            with suppress(Exception):
                await self.refresh_durable_state(database)
            await asyncio.sleep(STATE_REFRESH_SECONDS)


def model_role(job: ResearchJob, requested_model: str) -> str:
    for role, model in (job.models or {}).items():
        if model == requested_model and role in {"fast", "smart", "strategic"}:
            return role
    return "unknown"


def classify_error(error: Exception) -> str:
    if "timeout" in f"{type(error).__name__} {error}".lower():
        return "timeout"
    return "error"


def timer() -> float:
    return time.perf_counter()


def elapsed(started: float) -> float:
    return time.perf_counter() - started
