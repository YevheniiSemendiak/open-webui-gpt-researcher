from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from prometheus_client import generate_latest

from open_webui_gpt_researcher.db import ResearchArtifact, ResearchJob
from open_webui_gpt_researcher.domain import ResearchStage
from open_webui_gpt_researcher.metrics import ResearchMetrics, classify_error, model_role


def make_job(*, state: str = "succeeded") -> ResearchJob:
    now = datetime.now(UTC)
    return ResearchJob(
        id="00000000-0000-0000-0000-000000000001",
        user_id="user",
        idempotency_key="idempotency",
        request_hash="hash",
        query="query",
        chat_id="chat",
        message_id="message",
        sources=[],
        context_documents=[],
        iteration=1,
        research={"strategy": "focused"},
        budget={"max_queries": 5, "max_wall_time_seconds": 300},
        models={"fast": "small", "smart": "large", "strategic": "large"},
        model_capabilities=[],
        report_type="deep",
        report_formats=["markdown"],
        state=state,
        usage={
            "input_tokens": 100,
            "output_tokens": 20,
            "searches": 3,
            "private_searches": 1,
            "estimated_token_calls": 1,
        },
        created_at=now - timedelta(seconds=30),
        started_at=now - timedelta(seconds=20),
        finished_at=now,
    )


def test_job_observations_and_bounded_labels() -> None:
    metrics = ResearchMetrics()
    job = make_job()

    metrics.observe_progress({"stage": ResearchStage.PLANNING.value})
    metrics.observe_progress({"stage": "unbounded-upstream-stage"})

    output = generate_latest(metrics.registry).decode()
    assert 'deep_research_progress_events_total{stage="planning"} 1.0' in output
    assert 'deep_research_progress_events_total{stage="other"} 1.0' in output
    assert model_role(job, "small") == "fast"
    assert model_role(job, "missing") == "unknown"


def test_error_classification() -> None:
    assert classify_error(TimeoutError()) == "timeout"
    assert classify_error(RuntimeError()) == "error"


async def test_durable_state_refresh(
    api_client: tuple[httpx.AsyncClient, Any],
) -> None:
    client, app = api_client
    await app.state.metrics.refresh_durable_state(app.state.database)

    output = generate_latest(app.state.metrics.registry).decode()
    assert 'deep_research_jobs_current{state="pending"} 0.0' in output
    assert "deep_research_oldest_pending_age_seconds 0.0" in output

    job = make_job()
    async with app.state.database.session() as session, session.begin():
        session.add(job)
        session.add(
            ResearchArtifact(
                job_id=job.id,
                name="report.md",
                object_key="jobs/report.md",
                media_type="text/markdown",
                size=1_024,
            )
        )
    await app.state.metrics.refresh_durable_state(app.state.database)

    output = generate_latest(app.state.metrics.registry).decode()
    assert 'deep_research_jobs_current{state="succeeded"} 1.0' in output
    assert 'deep_research_jobs_retained{state="succeeded",strategy="focused"} 1.0' in output
    assert (
        'deep_research_jobs_completed_recent{state="succeeded",strategy="focused",window="1h"}'
        " 1.0" in output
    )
    assert 'deep_research_job_queue_wait_seconds{quantile="0.95",strategy="focused"} 10.0' in output
    assert (
        'deep_research_job_execution_duration_seconds{quantile="0.95",state="succeeded",strategy="focused"}'
        " 20.0" in output
    )
    assert 'deep_research_usage_retained_total{kind="input_tokens"} 100.0' in output
    assert "deep_research_artifacts_current 1.0" in output
    assert "deep_research_artifact_bytes_current 1024.0" in output

    response = await client.get("/metrics")
    assert response.status_code == 200
    assert "deep_research_jobs_current" in response.text
