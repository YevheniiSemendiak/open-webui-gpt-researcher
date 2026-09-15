from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

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
from open_webui_gpt_researcher.repository import JobRepository
from open_webui_gpt_researcher.runner import Runner


class RecordingExecutor:
    def __init__(self, error: Exception | None = None) -> None:
        self.calls: list[tuple[UUID, str]] = []
        self.error = error

    async def submit(self, *, job_id: UUID, runner_token: str) -> None:
        self.calls.append((job_id, runner_token))
        if self.error:
            raise self.error


async def test_controller_dispatches_and_records_failure(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'controller.sqlite'}")
    await database.create_all()
    repository = JobRepository()
    request = CreateJobRequest(query="Research queues", chat_id="c", message_id="m")
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
    async with database.session() as session:
        stored = await session.get(type(job), job_id)
        assert stored is not None
        assert stored.state == JobState.FAILED.value
        assert "scheduler unavailable" in str(stored.error)
    assert await controller.dispatch_one() is False
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


async def test_controller_can_disable_election_for_tests(monkeypatch: Any) -> None:
    controller = Controller(
        settings=Settings(controller_leader_election="none"),
        database=FakeLeaderDatabase(FakeLeaderLease(acquired=False)),  # type: ignore[arg-type]
        repository=JobRepository(),
        executor=RecordingExecutor(),
    )
    dispatch = AsyncMock()
    monkeypatch.setattr(controller, "_dispatch_forever", dispatch)
    await controller.run_forever()
    dispatch.assert_awaited_once_with()


class FakeRunnerClient:
    def __init__(self, state: JobState = JobState.RUNNING) -> None:
        self.token = "runner-token"
        self.spec = RunnerJobSpec(
            id=uuid4(),
            query="Test runner",
            sources=[{"kind": "collection", "id": "test-knowledge"}],
            budget=ResearchBudget(max_wall_time_seconds=60),
            model_profile="default",
            report_type="deep",
            report_formats=["markdown"],
        )
        self.current_state = state
        self.events: list[tuple[str, dict[str, object]]] = []
        self.completion: RunnerCompletion | None = None
        self.was_started = False
        self.was_cancelled = False
        self.was_closed = False

    async def get_spec(self) -> RunnerJobSpec:
        return self.spec

    async def started(self) -> None:
        self.was_started = True

    async def event(self, event_type: str, data: dict[str, object]) -> None:
        self.events.append((event_type, data))

    async def retrieve_private_context(self, query: str) -> list[dict[str, object]]:
        return [{"text": query}]

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


async def test_runner_completes_and_configures_budgets(monkeypatch: Any) -> None:
    client = FakeRunnerClient()
    settings = Settings(model_profiles={"default": "model"}, runner_cancel_poll_seconds=0.5)
    monkeypatch.setenv("RESEARCH_INTERNAL_BASE_URL", "http://api")
    await Runner(settings=settings, client=client, engine=InstantEngine()).run()  # type: ignore[arg-type]
    assert client.was_started and client.was_closed
    assert client.completion is not None
    assert client.completion.sources == [{"text": "Test runner"}]


async def test_runner_honors_cancel_request() -> None:
    client = FakeRunnerClient(JobState.CANCEL_REQUESTED)
    settings = Settings(model_profiles={"default": "model"}, runner_cancel_poll_seconds=0.5)
    await Runner(settings=settings, client=client, engine=SlowEngine()).run()  # type: ignore[arg-type]
    assert client.was_cancelled and client.was_closed
