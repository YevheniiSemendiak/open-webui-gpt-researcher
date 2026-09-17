from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import httpx
import pytest

from open_webui_gpt_researcher.config import Settings
from open_webui_gpt_researcher.controller import Controller
from open_webui_gpt_researcher.db import Database
from open_webui_gpt_researcher.domain import (
    CreateJobRequest,
    JobState,
    ResearchBudget,
    RunnerCompletion,
    RunnerJobSpec,
)
from open_webui_gpt_researcher.executors import DispatchStatus
from open_webui_gpt_researcher.repository import JobRepository
from open_webui_gpt_researcher.runner import Runner

MODEL_REQUEST = {
    "models": {"fast": "test-model", "smart": "test-model", "strategic": "test-model"},
    "model_capabilities": [
        {"id": "test-model", "context_length": 128_000, "max_output_tokens": 32_000}
    ],
}


class RecordingExecutor:
    def __init__(
        self,
        error: Exception | None = None,
        status: DispatchStatus = DispatchStatus.MISSING,
    ) -> None:
        self.calls: list[tuple[UUID, str, int]] = []
        self.stops: list[tuple[UUID, int]] = []
        self.error = error
        self.status = status

    async def submit(self, *, job_id: UUID, runner_token: str, attempt: int) -> None:
        self.calls.append((job_id, runner_token, attempt))
        if self.error:
            raise self.error

    async def inspect(self, *, job_id: UUID, attempt: int) -> DispatchStatus:
        del job_id, attempt
        return self.status

    async def stop(self, *, job_id: UUID, attempt: int) -> None:
        self.stops.append((job_id, attempt))


async def test_controller_dispatches_and_records_failure(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'controller.sqlite'}")
    await database.create_all()
    repository = JobRepository()
    request = CreateJobRequest(
        query="Research queues", chat_id="c", message_id="m", **MODEL_REQUEST
    )
    async with database.session() as session, session.begin():
        job, _ = await repository.create_job(
            session, user_id="u", idempotency_key="controller-test", request=request
        )
        job_id = job.id
    executor = RecordingExecutor(RuntimeError("scheduler unavailable"))
    controller = Controller(
        settings=Settings(database_url="sqlite+aiosqlite://"),
        database=database,
        repository=repository,
        executor=executor,
    )
    assert await controller.dispatch_one() is True
    assert executor.calls[0][0] == UUID(job_id)
    assert executor.calls[0][2] == 1
    async with database.session() as session:
        stored = await session.get(type(job), job_id)
        assert stored is not None
        assert stored.state == JobState.FAILED.value
        assert "scheduler unavailable" in str(stored.error)
    assert await controller.dispatch_one() is False
    await database.close()


async def test_controller_requeues_missing_expired_dispatch(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'reconcile.sqlite'}")
    await database.create_all()
    repository = JobRepository()
    async with database.session() as session, session.begin():
        job, _ = await repository.create_job(
            session,
            user_id="u",
            idempotency_key="reconcile-test",
            request=CreateJobRequest(
                query="Recover dispatch", chat_id="c", message_id="m", **MODEL_REQUEST
            ),
        )
        claim = await repository.claim_next(
            session,
            max_concurrent_jobs=5,
            lease_seconds=120,
        )
        assert claim is not None
        job.dispatch_lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)

    controller = Controller(
        settings=Settings(database_url="sqlite+aiosqlite://"),
        database=database,
        repository=repository,
        executor=RecordingExecutor(status=DispatchStatus.MISSING),
    )
    assert await controller.reconcile_expired_dispatches() == 1
    async with database.session() as session:
        stored = await session.get(type(job), job.id)
        assert stored is not None
        assert stored.state == JobState.PENDING.value
        assert stored.runner_token_hash is None
        assert stored.dispatch_lease_expires_at is None

    assert await controller.dispatch_one() is True
    async with database.session() as session:
        stored = await session.get(type(job), job.id)
        assert stored is not None
        assert stored.attempt == 2
    await database.close()


async def test_controller_renews_existing_expired_dispatch(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'renew.sqlite'}")
    await database.create_all()
    repository = JobRepository()
    expired = datetime.now(UTC) - timedelta(seconds=1)
    async with database.session() as session, session.begin():
        job, _ = await repository.create_job(
            session,
            user_id="u",
            idempotency_key="renew-test",
            request=CreateJobRequest(
                query="Renew dispatch", chat_id="c", message_id="m", **MODEL_REQUEST
            ),
        )
        claim = await repository.claim_next(
            session,
            max_concurrent_jobs=5,
            lease_seconds=120,
        )
        assert claim is not None
        job.dispatch_lease_expires_at = expired

    controller = Controller(
        settings=Settings(database_url="sqlite+aiosqlite://"),
        database=database,
        repository=repository,
        executor=RecordingExecutor(status=DispatchStatus.ACTIVE),
    )
    assert await controller.reconcile_expired_dispatches() == 1
    async with database.session() as session:
        stored = await session.get(type(job), job.id)
        assert stored is not None
        assert stored.state == JobState.DISPATCHED.value
        assert stored.dispatch_lease_expires_at is not None
        assert stored.dispatch_lease_expires_at > expired.replace(tzinfo=None)
    await database.close()


@pytest.mark.parametrize(
    ("dispatch_status", "expected_stops"),
    [
        (DispatchStatus.MISSING, []),
        (DispatchStatus.ACTIVE, [1]),
    ],
)
async def test_controller_finishes_cancelled_dispatch(
    tmp_path: Path,
    dispatch_status: DispatchStatus,
    expected_stops: list[int],
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'cancel-reconcile.sqlite'}")
    await database.create_all()
    repository = JobRepository()
    async with database.session() as session, session.begin():
        job, _ = await repository.create_job(
            session,
            user_id="u",
            idempotency_key="cancel-reconcile-test",
            request=CreateJobRequest(
                query="Cancel dispatch", chat_id="c", message_id="m", **MODEL_REQUEST
            ),
        )
        claim = await repository.claim_next(
            session,
            max_concurrent_jobs=5,
            lease_seconds=120,
        )
        assert claim is not None
        await repository.request_cancel(session, job_id=UUID(job.id), user_id="u")
        job.dispatch_lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)

    executor = RecordingExecutor(status=dispatch_status)
    controller = Controller(
        settings=Settings(database_url="sqlite+aiosqlite://"),
        database=database,
        repository=repository,
        executor=executor,
    )
    assert await controller.reconcile_expired_dispatches() == 1
    async with database.session() as session:
        stored = await session.get(type(job), job.id)
        assert stored is not None
        assert stored.state == JobState.CANCELLED.value
        assert stored.finished_at is not None
    assert [attempt for _, attempt in executor.stops] == expected_stops
    await database.close()


async def test_controller_requeues_stale_running_job_and_preserves_budget_usage(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'stale-runner.sqlite'}")
    await database.create_all()
    repository = JobRepository()
    async with database.session() as session, session.begin():
        job, _ = await repository.create_job(
            session,
            user_id="u",
            idempotency_key="stale-runner-test",
            request=CreateJobRequest(
                query="Recover runner", chat_id="c", message_id="m", **MODEL_REQUEST
            ),
        )
        claim = await repository.claim_next(session, max_concurrent_jobs=5, lease_seconds=120)
        assert claim is not None
        await repository.mark_started(session, job_id=claim.id, runner_token=claim.runner_token)
        job.usage = {"searches": 3}
        job.updated_at = datetime.now(UTC) - timedelta(seconds=120)

    executor = RecordingExecutor(status=DispatchStatus.MISSING)
    controller = Controller(
        settings=Settings(
            database_url="sqlite+aiosqlite://",
            runner_heartbeat_seconds=1,
            runner_stale_seconds=10,
            runner_max_attempts=2,
        ),
        database=database,
        repository=repository,
        executor=executor,
    )
    assert await controller.reconcile_stale_runners() == 1
    async with database.session() as session:
        stored = await session.get(type(job), job.id)
        assert stored is not None
        assert stored.state == JobState.PENDING.value
        assert stored.runner_token_hash is None
        assert stored.started_at is None
        assert stored.usage == {"searches": 3}

    assert await controller.dispatch_one() is True
    async with database.session() as session:
        stored = await session.get(type(job), job.id)
        assert stored is not None
        assert stored.attempt == 2
    await database.close()


async def test_controller_stops_and_fails_stale_runner_after_attempt_limit(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'stale-limit.sqlite'}")
    await database.create_all()
    repository = JobRepository()
    async with database.session() as session, session.begin():
        job, _ = await repository.create_job(
            session,
            user_id="u",
            idempotency_key="stale-limit-test",
            request=CreateJobRequest(
                query="Fail stale runner", chat_id="c", message_id="m", **MODEL_REQUEST
            ),
        )
        claim = await repository.claim_next(session, max_concurrent_jobs=5, lease_seconds=120)
        assert claim is not None
        await repository.mark_started(session, job_id=claim.id, runner_token=claim.runner_token)
        job.updated_at = datetime.now(UTC) - timedelta(seconds=120)

    executor = RecordingExecutor(status=DispatchStatus.ACTIVE)
    controller = Controller(
        settings=Settings(
            database_url="sqlite+aiosqlite://",
            runner_heartbeat_seconds=1,
            runner_stale_seconds=10,
            runner_max_attempts=1,
        ),
        database=database,
        repository=repository,
        executor=executor,
    )
    assert await controller.reconcile_stale_runners() == 1
    assert executor.stops == [(UUID(job.id), 1)]
    async with database.session() as session:
        stored = await session.get(type(job), job.id)
        assert stored is not None
        assert stored.state == JobState.FAILED.value
        assert "heartbeat expired" in str(stored.error)
    await database.close()


class FakeLeaderLease:
    def __init__(self, *, acquired: bool, valid: bool = True) -> None:
        self.acquired = acquired
        self.valid = valid
        self.backend_pid = 1234

    async def is_valid(self) -> bool:
        return self.valid


class FakeLeaderDatabase:
    def __init__(self, lease: FakeLeaderLease) -> None:
        self.lease = lease
        self.lock_ids: list[int] = []

    @asynccontextmanager
    async def advisory_lock(self, lock_id: int) -> Any:
        self.lock_ids.append(lock_id)
        yield self.lease


async def test_controller_uses_database_leadership(monkeypatch: Any) -> None:
    database = FakeLeaderDatabase(FakeLeaderLease(acquired=True))
    controller = Controller(
        settings=Settings(),
        database=database,  # type: ignore[arg-type]
        repository=JobRepository(),
        executor=RecordingExecutor(),
    )
    dispatch = AsyncMock(side_effect=asyncio.CancelledError)
    monkeypatch.setattr(controller, "_dispatch_forever", dispatch)
    with pytest.raises(asyncio.CancelledError):
        await controller.run_forever()
    assert database.lock_ids == [controller.settings.controller_leader_lock_id]
    dispatch.assert_awaited_once_with(lease=database.lease)


async def test_controller_returns_when_leadership_is_lost() -> None:
    controller = Controller(
        settings=Settings(),
        database=FakeLeaderDatabase(FakeLeaderLease(acquired=True)),  # type: ignore[arg-type]
        repository=JobRepository(),
        executor=RecordingExecutor(),
    )
    await controller._dispatch_forever(  # type: ignore[arg-type]
        lease=FakeLeaderLease(acquired=True, valid=False)
    )


class FakeRunnerClient:
    def __init__(self, state: JobState = JobState.RUNNING) -> None:
        self.token = "runner-token"
        self.spec = RunnerJobSpec(
            id=uuid4(),
            query="Test runner",
            sources=[{"kind": "collection", "id": "test-knowledge"}],
            context_documents=[
                {
                    "kind": "conversation",
                    "id": "chat-1",
                    "chat_id": "chat-1",
                    "title": "Current chat",
                    "text": "Prior conversation",
                }
            ],
            budget=ResearchBudget(max_wall_time_seconds=60),
            models={"fast": "test-model", "smart": "test-model", "strategic": "test-model"},
            model_capabilities=[
                {
                    "id": "test-model",
                    "context_length": 128_000,
                    "max_output_tokens": 32_000,
                }
            ],
            report_type="deep",
            report_formats=["markdown"],
        )
        self.current_state = state
        self.events: list[tuple[str, dict[str, object]]] = []
        self.completion: RunnerCompletion | None = None
        self.was_started = False
        self.was_cancelled = False
        self.was_closed = False
        self.heartbeats = 0

    async def get_spec(self) -> RunnerJobSpec:
        return self.spec

    async def started(self) -> None:
        self.was_started = True

    async def event(self, event_type: str, data: dict[str, object]) -> None:
        self.events.append((event_type, data))

    async def heartbeat(self) -> None:
        self.heartbeats += 1

    async def state(self) -> JobState:
        await asyncio.sleep(0)
        return self.current_state

    async def complete(self, completion: RunnerCompletion) -> None:
        self.completion = completion

    async def failed(self, error: str) -> None:
        raise AssertionError(error)

    async def cancelled(self) -> None:
        self.was_cancelled = True

    async def close(self) -> None:
        self.was_closed = True


class InstantEngine:
    async def run(
        self,
        spec: RunnerJobSpec,
        *,
        private_context: list[dict[str, object]],
        progress: Any,
    ) -> RunnerCompletion:
        await progress("research.progress", {"stage": "done"})
        return RunnerCompletion(report_markdown=f"# {spec.query}", sources=private_context)


class SlowEngine:
    async def run(self, *_: Any, **__: Any) -> RunnerCompletion:
        await asyncio.sleep(60)
        raise AssertionError("unreachable")


async def test_runner_completes_and_configures_research_shape(monkeypatch: Any) -> None:
    client = FakeRunnerClient()
    settings = Settings(default_model_profiles={"default": "model"}, runner_cancel_poll_seconds=0.5)
    monkeypatch.setenv("INTERNAL_BASE_URL", "http://api")
    await Runner(settings=settings, client=client, engine=InstantEngine()).run()  # type: ignore[arg-type]
    assert client.was_started and client.was_closed
    assert client.completion is not None
    assert client.completion.sources == [
        {
            "text": "Prior conversation",
            "metadata": {
                "source": "openwebui-chat:chat-1",
                "file_id": "chat-1",
                "name": "Current chat",
                "kind": "conversation",
            },
        }
    ]
    assert client.events[0] == (
        "research.progress",
        {
            "stage": "retrieval",
            "file_sources": 0,
            "knowledge_sources": 1,
            "context_items": 1,
        },
    )
    assert os.environ["DEEP_RESEARCH_BREADTH"] == "2"
    assert os.environ["DEEP_RESEARCH_DEPTH"] == "2"
    assert os.environ["MAX_ITERATIONS"] == "2"
    assert os.environ["FAST_LLM"] == "openai:test-model"
    assert os.environ["SMART_LLM"] == "openai:test-model"
    assert os.environ["STRATEGIC_LLM"] == "openai:test-model"
    assert os.environ["OPENAI_API_KEY"] == client.token


async def test_runner_honors_cancel_request() -> None:
    client = FakeRunnerClient(JobState.CANCEL_REQUESTED)
    settings = Settings(default_model_profiles={"default": "model"}, runner_cancel_poll_seconds=0.5)
    await Runner(settings=settings, client=client, engine=SlowEngine()).run()  # type: ignore[arg-type]
    assert client.was_cancelled and client.was_closed


async def test_runner_tolerates_transient_cancellation_poll_disconnect() -> None:
    client = FakeRunnerClient(JobState.CANCEL_REQUESTED)
    calls = 0

    async def flaky_state() -> JobState:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.RemoteProtocolError("server disconnected")
        return JobState.CANCEL_REQUESTED

    client.state = flaky_state  # type: ignore[method-assign]
    settings = Settings(default_model_profiles={"default": "model"}, runner_cancel_poll_seconds=0.5)
    runner = Runner(settings=settings, client=client, engine=SlowEngine())  # type: ignore[arg-type]
    assert await runner._wait_for_stop() == JobState.CANCEL_REQUESTED
    assert calls == 2
