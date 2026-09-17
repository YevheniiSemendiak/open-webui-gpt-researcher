from __future__ import annotations

import hashlib
import json
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID

from sqlalchemy import delete, exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .db import ResearchArtifact, ResearchEvent, ResearchJob
from .domain import (
    TERMINAL_STATES,
    ContextDocument,
    CreateJobRequest,
    JobEventView,
    JobState,
    JobView,
    RunnerCompletion,
    RunnerJobSpec,
    SourceRef,
)


class JobNotFoundError(LookupError):
    pass


class IdempotencyConflictError(ValueError):
    pass


class InvalidStateError(ValueError):
    pass


def hash_token(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def request_digest(request: CreateJobRequest) -> str:
    payload = request.model_dump(mode="json")
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def as_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


@dataclass(frozen=True)
class ClaimedJob:
    id: UUID
    runner_token: str
    attempt: int


@dataclass(frozen=True)
class ExpiredDispatch:
    id: UUID
    attempt: int
    state: JobState


@dataclass(frozen=True)
class StaleRunner:
    id: UUID
    attempt: int
    state: JobState
    updated_at: datetime


class JobRepository:
    """Transactional persistence and state transitions."""

    async def create_job(
        self,
        session: AsyncSession,
        *,
        user_id: str,
        idempotency_key: str,
        request: CreateJobRequest,
    ) -> tuple[ResearchJob, bool]:
        digest = request_digest(request)
        existing = await session.scalar(
            select(ResearchJob).where(
                ResearchJob.user_id == user_id,
                ResearchJob.idempotency_key == idempotency_key,
            )
        )
        if existing is not None:
            if existing.request_hash != digest:
                raise IdempotencyConflictError
            return existing, False

        parent = await session.scalar(
            select(ResearchJob)
            .where(
                ResearchJob.user_id == user_id,
                ResearchJob.chat_id == request.chat_id,
                ResearchJob.state == JobState.SUCCEEDED.value,
            )
            .order_by(ResearchJob.created_at.desc())
            .limit(1)
        )

        job = ResearchJob(
            user_id=user_id,
            idempotency_key=idempotency_key,
            request_hash=digest,
            query=request.query,
            chat_id=request.chat_id,
            message_id=request.message_id,
            sources=[source.model_dump(mode="json") for source in request.sources],
            context_documents=[
                document.model_dump(mode="json") for document in request.context_documents
            ],
            parent_job_id=parent.id if parent is not None else None,
            iteration=(parent.iteration + 1) if parent is not None else 1,
            research=request.research.model_dump(mode="json"),
            budget=request.budget.model_dump(mode="json"),
            models=request.models.model_dump(mode="json"),
            model_capabilities=[
                capability.model_dump(mode="json") for capability in request.model_capabilities
            ],
            report_type=request.report_type,
            report_formats=request.report_formats,
            state=JobState.PENDING.value,
        )
        session.add(job)
        await session.flush()
        await self._append_event_locked(session, job, "job.created", {"state": job.state})
        return job, True

    async def get_for_user(
        self, session: AsyncSession, *, job_id: UUID, user_id: str
    ) -> ResearchJob:
        job = await session.scalar(
            select(ResearchJob).where(ResearchJob.id == str(job_id), ResearchJob.user_id == user_id)
        )
        if job is None:
            raise JobNotFoundError
        return job

    async def get_for_runner(
        self, session: AsyncSession, *, job_id: UUID, runner_token: str
    ) -> ResearchJob:
        job = await session.get(ResearchJob, str(job_id))
        if job is None or job.runner_token_hash is None:
            raise JobNotFoundError
        if not secrets.compare_digest(job.runner_token_hash, hash_token(runner_token)):
            raise JobNotFoundError
        return job

    async def get_for_runner_locked(
        self, session: AsyncSession, *, job_id: UUID, runner_token: str
    ) -> ResearchJob:
        job = await session.scalar(
            select(ResearchJob).where(ResearchJob.id == str(job_id)).with_for_update()
        )
        if job is None or job.runner_token_hash is None:
            raise JobNotFoundError
        if not secrets.compare_digest(job.runner_token_hash, hash_token(runner_token)):
            raise JobNotFoundError
        return job

    async def get_by_idempotency(
        self, session: AsyncSession, *, user_id: str, idempotency_key: str
    ) -> ResearchJob:
        job = await session.scalar(
            select(ResearchJob).where(
                ResearchJob.user_id == user_id,
                ResearchJob.idempotency_key == idempotency_key,
            )
        )
        if job is None:
            raise JobNotFoundError
        return job

    async def get_by_chat_message(
        self,
        session: AsyncSession,
        *,
        user_id: str,
        chat_id: str,
        message_id: str,
    ) -> ResearchJob:
        job = await session.scalar(
            select(ResearchJob)
            .where(
                ResearchJob.user_id == user_id,
                ResearchJob.chat_id == chat_id,
                ResearchJob.message_id == message_id,
            )
            .order_by(ResearchJob.created_at.desc())
            .limit(1)
        )
        if job is None:
            raise JobNotFoundError
        return job

    async def claim_next(
        self,
        session: AsyncSession,
        *,
        max_concurrent_jobs: int,
        lease_seconds: int,
    ) -> ClaimedJob | None:
        active = await session.scalar(
            select(func.count())
            .select_from(ResearchJob)
            .where(
                ResearchJob.state.in_(
                    [
                        JobState.DISPATCHED.value,
                        JobState.RUNNING.value,
                        JobState.CANCEL_REQUESTED.value,
                    ]
                )
            )
        )
        if (active or 0) >= max_concurrent_jobs:
            return None

        job = await session.scalar(
            select(ResearchJob)
            .where(ResearchJob.state == JobState.PENDING.value)
            .order_by(ResearchJob.created_at)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if job is None:
            return None

        now = datetime.now(UTC)
        token = secrets.token_urlsafe(32)
        job.runner_token_hash = hash_token(token)
        job.state = JobState.DISPATCHED.value
        job.attempt += 1
        job.dispatch_lease_expires_at = now + timedelta(seconds=lease_seconds)
        job.updated_at = now
        await self._append_event_locked(session, job, "job.dispatched", {"attempt": job.attempt})
        return ClaimedJob(id=UUID(job.id), runner_token=token, attempt=job.attempt)

    async def list_expired_dispatches(
        self, session: AsyncSession, *, limit: int
    ) -> list[ExpiredDispatch]:
        now = datetime.now(UTC)
        jobs = (
            await session.scalars(
                select(ResearchJob)
                .where(
                    ResearchJob.state.in_(
                        [JobState.DISPATCHED.value, JobState.CANCEL_REQUESTED.value]
                    ),
                    ResearchJob.started_at.is_(None),
                    ResearchJob.dispatch_lease_expires_at.is_not(None),
                    ResearchJob.dispatch_lease_expires_at <= now,
                )
                .order_by(ResearchJob.dispatch_lease_expires_at)
                .limit(limit)
            )
        ).all()
        return [
            ExpiredDispatch(id=UUID(job.id), attempt=job.attempt, state=JobState(job.state))
            for job in jobs
        ]

    async def reconcile_expired_dispatch(
        self,
        session: AsyncSession,
        *,
        dispatch: ExpiredDispatch,
        action: Literal["renew", "requeue", "fail", "cancel"],
        lease_seconds: int,
        error: str = "",
    ) -> bool:
        job = await session.scalar(
            select(ResearchJob)
            .where(
                ResearchJob.id == str(dispatch.id),
                ResearchJob.attempt == dispatch.attempt,
                ResearchJob.state.in_([JobState.DISPATCHED.value, JobState.CANCEL_REQUESTED.value]),
                ResearchJob.started_at.is_(None),
            )
            .with_for_update()
        )
        if job is None:
            return False

        now = datetime.now(UTC)
        if action == "renew":
            job.dispatch_lease_expires_at = now + timedelta(seconds=lease_seconds)
            job.updated_at = now
            return True

        if job.state == JobState.CANCEL_REQUESTED.value:
            job.state = JobState.CANCELLED.value
            job.finished_at = now
            event_type = "job.cancelled"
            data: dict[str, object] = {"reason": "dispatch reconciliation"}
        elif action == "requeue":
            job.state = JobState.PENDING.value
            job.runner_token_hash = None
            event_type = "job.dispatch_requeued"
            data = {"attempt": job.attempt}
        else:
            job.state = JobState.FAILED.value
            job.error = error[:20_000] or "runner exited before reporting its state"
            job.finished_at = now
            event_type = "job.failed"
            data = {"error": job.error}
        job.dispatch_lease_expires_at = None
        job.updated_at = now
        await self._append_event_locked(session, job, event_type, data)
        return True

    async def list_stale_runners(
        self, session: AsyncSession, *, stale_seconds: int, limit: int
    ) -> list[StaleRunner]:
        cutoff = datetime.now(UTC) - timedelta(seconds=stale_seconds)
        jobs = (
            await session.scalars(
                select(ResearchJob)
                .where(
                    ResearchJob.state.in_(
                        [JobState.RUNNING.value, JobState.CANCEL_REQUESTED.value]
                    ),
                    ResearchJob.started_at.is_not(None),
                    ResearchJob.updated_at <= cutoff,
                )
                .order_by(ResearchJob.updated_at)
                .limit(limit)
            )
        ).all()
        return [
            StaleRunner(
                id=UUID(job.id),
                attempt=job.attempt,
                state=JobState(job.state),
                updated_at=job.updated_at,
            )
            for job in jobs
        ]

    async def reconcile_stale_runner(
        self,
        session: AsyncSession,
        *,
        runner: StaleRunner,
        action: Literal["refresh", "retry", "fail", "cancel"],
        error: str = "",
    ) -> bool:
        job = await session.scalar(
            select(ResearchJob)
            .where(
                ResearchJob.id == str(runner.id),
                ResearchJob.attempt == runner.attempt,
                ResearchJob.state.in_([JobState.RUNNING.value, JobState.CANCEL_REQUESTED.value]),
                ResearchJob.updated_at <= runner.updated_at,
            )
            .with_for_update()
        )
        if job is None:
            return False

        now = datetime.now(UTC)
        if action == "refresh":
            job.updated_at = now
            return True

        if action == "cancel" or job.state == JobState.CANCEL_REQUESTED.value:
            job.state = JobState.CANCELLED.value
            job.finished_at = now
            event_type = "job.cancelled"
            data: dict[str, object] = {"reason": "stale runner reconciliation"}
        elif action == "retry":
            wall_time = int(job.budget["max_wall_time_seconds"])
            if now >= as_utc(job.created_at) + timedelta(seconds=wall_time):
                job.state = JobState.FAILED.value
                job.error = "wall-time budget exhausted before runner recovery"
                job.finished_at = now
                event_type = "job.failed"
                data = {"error": job.error}
            else:
                job.state = JobState.PENDING.value
                job.runner_token_hash = None
                job.started_at = None
                job.finished_at = None
                job.error = None
                event_type = "job.runner_requeued"
                data = {"attempt": job.attempt, "reason": error or "runner disappeared"}
        else:
            job.state = JobState.FAILED.value
            job.error = error[:20_000] or "runner stopped sending heartbeats"
            job.finished_at = now
            event_type = "job.failed"
            data = {"error": job.error}
        job.dispatch_lease_expires_at = None
        job.updated_at = now
        await self._append_event_locked(session, job, event_type, data)
        return True

    async def list_events(
        self,
        session: AsyncSession,
        *,
        job_id: UUID,
        user_id: str,
        after: int = 0,
    ) -> list[ResearchEvent]:
        await self.get_for_user(session, job_id=job_id, user_id=user_id)
        return list(
            (
                await session.scalars(
                    select(ResearchEvent)
                    .where(
                        ResearchEvent.job_id == str(job_id),
                        ResearchEvent.sequence > after,
                    )
                    .order_by(ResearchEvent.sequence)
                )
            ).all()
        )

    async def append_runner_event(
        self,
        session: AsyncSession,
        *,
        job_id: UUID,
        runner_token: str,
        event_type: str,
        data: dict[str, object],
    ) -> ResearchEvent:
        job = await self.get_for_runner(session, job_id=job_id, runner_token=runner_token)
        if JobState(job.state) in {JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELLED}:
            raise InvalidStateError("cannot append to a terminal job")
        job.updated_at = datetime.now(UTC)
        return await self._append_event_locked(session, job, event_type, data)

    async def heartbeat(
        self, session: AsyncSession, *, job_id: UUID, runner_token: str
    ) -> ResearchJob:
        job = await self.get_for_runner_locked(session, job_id=job_id, runner_token=runner_token)
        if JobState(job.state) not in {JobState.RUNNING, JobState.CANCEL_REQUESTED}:
            raise InvalidStateError(f"cannot heartbeat job in state {job.state}")
        job.updated_at = datetime.now(UTC)
        return job

    async def mark_started(
        self, session: AsyncSession, *, job_id: UUID, runner_token: str
    ) -> ResearchJob:
        job = await self.get_for_runner(session, job_id=job_id, runner_token=runner_token)
        state = JobState(job.state)
        if state not in {JobState.DISPATCHED, JobState.RUNNING, JobState.CANCEL_REQUESTED}:
            raise InvalidStateError(f"cannot start job in state {job.state}")
        if state == JobState.CANCEL_REQUESTED:
            return job
        if state != JobState.RUNNING:
            now = datetime.now(UTC)
            job.state = JobState.RUNNING.value
            job.dispatch_lease_expires_at = None
            job.started_at = now
            job.updated_at = now
            await self._append_event_locked(session, job, "job.started", {})
        return job

    async def request_cancel(
        self, session: AsyncSession, *, job_id: UUID, user_id: str
    ) -> ResearchJob:
        job = await self.get_for_user(session, job_id=job_id, user_id=user_id)
        state = JobState(job.state)
        if state == JobState.PENDING:
            job.state = JobState.CANCELLED.value
            job.finished_at = datetime.now(UTC)
            job.updated_at = job.finished_at
            await self._append_event_locked(session, job, "job.cancelled", {})
        elif state in {JobState.DISPATCHED, JobState.RUNNING}:
            job.state = JobState.CANCEL_REQUESTED.value
            job.updated_at = datetime.now(UTC)
            await self._append_event_locked(session, job, "job.cancel_requested", {})
        return job

    async def mark_cancelled(
        self, session: AsyncSession, *, job_id: UUID, runner_token: str
    ) -> ResearchJob:
        job = await self.get_for_runner(session, job_id=job_id, runner_token=runner_token)
        if job.state == JobState.CANCELLED.value:
            return job
        if JobState(job.state) not in {JobState.CANCEL_REQUESTED, JobState.RUNNING}:
            raise InvalidStateError(f"cannot cancel job in state {job.state}")
        job.state = JobState.CANCELLED.value
        job.finished_at = datetime.now(UTC)
        job.updated_at = job.finished_at
        job.dispatch_lease_expires_at = None
        await self._append_event_locked(session, job, "job.cancelled", {})
        return job

    async def mark_failed(
        self,
        session: AsyncSession,
        *,
        job_id: UUID,
        runner_token: str,
        error: str,
    ) -> ResearchJob:
        job = await self.get_for_runner(session, job_id=job_id, runner_token=runner_token)
        if job.state == JobState.FAILED.value:
            return job
        if JobState(job.state) not in {JobState.DISPATCHED, JobState.RUNNING}:
            raise InvalidStateError(f"cannot fail job in state {job.state}")
        job.state = JobState.FAILED.value
        job.error = error[:20_000]
        job.finished_at = datetime.now(UTC)
        job.updated_at = job.finished_at
        job.dispatch_lease_expires_at = None
        await self._append_event_locked(session, job, "job.failed", {"error": job.error})
        return job

    async def mark_completed(
        self,
        session: AsyncSession,
        *,
        job_id: UUID,
        runner_token: str,
        completion: RunnerCompletion,
        result: dict[str, object],
    ) -> ResearchJob:
        job = await self.get_for_runner(session, job_id=job_id, runner_token=runner_token)
        if job.state == JobState.CANCEL_REQUESTED.value:
            return await self.mark_cancelled(session, job_id=job_id, runner_token=runner_token)
        if job.state != JobState.RUNNING.value:
            raise InvalidStateError(f"cannot complete job in state {job.state}")
        job.state = JobState.SUCCEEDED.value
        job.result = result
        job.usage = {**(job.usage or {}), **completion.usage}
        job.finished_at = datetime.now(UTC)
        job.updated_at = job.finished_at
        job.dispatch_lease_expires_at = None
        await self._append_event_locked(session, job, "job.succeeded", result)
        return job

    async def add_artifact(
        self,
        session: AsyncSession,
        *,
        job_id: UUID,
        name: str,
        object_key: str,
        media_type: str,
        size: int,
    ) -> ResearchArtifact:
        artifact = ResearchArtifact(
            job_id=str(job_id),
            name=name,
            object_key=object_key,
            media_type=media_type,
            size=size,
        )
        session.add(artifact)
        await session.flush()
        return artifact

    async def consume_usage(
        self,
        session: AsyncSession,
        *,
        job_id: UUID,
        runner_token: str,
        input_tokens: int = 0,
        output_tokens: int = 0,
        searches: int = 0,
        private_searches: int = 0,
        estimated_token_calls: int = 0,
    ) -> dict[str, int]:
        job = await self.get_for_runner_locked(session, job_id=job_id, runner_token=runner_token)
        usage = dict(job.usage or {})
        usage["input_tokens"] = max(0, int(usage.get("input_tokens", 0)) + input_tokens)
        usage["output_tokens"] = max(0, int(usage.get("output_tokens", 0)) + output_tokens)
        usage["searches"] = max(0, int(usage.get("searches", 0)) + searches)
        usage["private_searches"] = max(0, int(usage.get("private_searches", 0)) + private_searches)
        usage["estimated_token_calls"] = max(
            0, int(usage.get("estimated_token_calls", 0)) + estimated_token_calls
        )
        job.usage = usage
        job.updated_at = datetime.now(UTC)
        await session.flush()
        return {key: int(value) for key, value in usage.items()}

    async def get_artifact(
        self, session: AsyncSession, *, job_id: UUID, name: str
    ) -> ResearchArtifact:
        artifact = await session.scalar(
            select(ResearchArtifact).where(
                ResearchArtifact.job_id == str(job_id), ResearchArtifact.name == name
            )
        )
        if artifact is None:
            raise JobNotFoundError
        return artifact

    async def list_artifacts(
        self, session: AsyncSession, *, job_id: UUID
    ) -> list[ResearchArtifact]:
        return list(
            (
                await session.scalars(
                    select(ResearchArtifact)
                    .where(ResearchArtifact.job_id == str(job_id))
                    .order_by(ResearchArtifact.id)
                )
            ).all()
        )

    async def list_expired_artifacts(
        self, session: AsyncSession, *, cutoff: datetime, limit: int
    ) -> list[ResearchArtifact]:
        return list(
            (
                await session.scalars(
                    select(ResearchArtifact)
                    .join(ResearchJob, ResearchArtifact.job_id == ResearchJob.id)
                    .where(
                        ResearchJob.state.in_([state.value for state in TERMINAL_STATES]),
                        ResearchJob.finished_at.is_not(None),
                        ResearchJob.finished_at < cutoff,
                    )
                    .order_by(ResearchArtifact.id)
                    .limit(limit)
                )
            ).all()
        )

    async def delete_artifact_record(self, session: AsyncSession, *, artifact_id: int) -> bool:
        result = await session.execute(
            delete(ResearchArtifact).where(ResearchArtifact.id == artifact_id)
        )
        return bool(getattr(result, "rowcount", 0))

    async def delete_expired_events(
        self, session: AsyncSession, *, cutoff: datetime, limit: int
    ) -> int:
        event_ids = (
            select(ResearchEvent.id)
            .join(ResearchJob, ResearchEvent.job_id == ResearchJob.id)
            .where(
                ResearchEvent.created_at < cutoff,
                ResearchJob.state.in_([state.value for state in TERMINAL_STATES]),
            )
            .order_by(ResearchEvent.id)
            .limit(limit)
        )
        result = await session.execute(delete(ResearchEvent).where(ResearchEvent.id.in_(event_ids)))
        return int(getattr(result, "rowcount", 0) or 0)

    async def delete_expired_jobs(
        self, session: AsyncSession, *, cutoff: datetime, limit: int
    ) -> int:
        job_ids = (
            select(ResearchJob.id)
            .where(
                ResearchJob.state.in_([state.value for state in TERMINAL_STATES]),
                ResearchJob.finished_at.is_not(None),
                ResearchJob.finished_at < cutoff,
                ~exists().where(ResearchArtifact.job_id == ResearchJob.id),
            )
            .order_by(ResearchJob.finished_at)
            .limit(limit)
        )
        result = await session.execute(delete(ResearchJob).where(ResearchJob.id.in_(job_ids)))
        return int(getattr(result, "rowcount", 0) or 0)

    async def list_artifact_keys(self, session: AsyncSession) -> set[str]:
        return set((await session.scalars(select(ResearchArtifact.object_key))).all())

    async def _append_event_locked(
        self,
        session: AsyncSession,
        job: ResearchJob,
        event_type: str,
        data: dict[str, object],
    ) -> ResearchEvent:
        await session.execute(
            select(ResearchJob.id).where(ResearchJob.id == job.id).with_for_update()
        )
        current = await session.scalar(
            select(func.max(ResearchEvent.sequence)).where(ResearchEvent.job_id == job.id)
        )
        event = ResearchEvent(
            job_id=job.id,
            sequence=(current or 0) + 1,
            event_type=event_type,
            data=data,
        )
        session.add(event)
        await session.flush()
        return event


def to_job_view(job: ResearchJob) -> JobView:
    return JobView(
        id=UUID(job.id),
        state=JobState(job.state),
        query=job.query,
        chat_id=job.chat_id,
        message_id=job.message_id,
        sources=[SourceRef.model_validate(source) for source in job.sources],
        parent_job_id=UUID(job.parent_job_id) if job.parent_job_id else None,
        iteration=job.iteration,
        context_document_count=len(job.context_documents or []),
        research=job.research,
        budget=job.budget,
        models=job.models,
        model_capabilities=job.model_capabilities,
        created_at=job.created_at,
        updated_at=job.updated_at,
        started_at=job.started_at,
        finished_at=job.finished_at,
        result=job.result,
        usage=job.usage,
        error=job.error,
    )


def to_event_view(event: ResearchEvent) -> JobEventView:
    return JobEventView(
        sequence=event.sequence,
        event_type=event.event_type,
        data=event.data,
        created_at=event.created_at,
    )


def to_runner_spec(job: ResearchJob) -> RunnerJobSpec:
    wall_time = int(job.budget["max_wall_time_seconds"])
    remaining_wall_time = max(
        1,
        int(
            (
                as_utc(job.created_at) + timedelta(seconds=wall_time) - datetime.now(UTC)
            ).total_seconds()
        ),
    )
    return RunnerJobSpec(
        id=UUID(job.id),
        query=job.query,
        sources=[SourceRef.model_validate(source) for source in job.sources],
        context_documents=[
            ContextDocument.model_validate(document) for document in job.context_documents
        ],
        parent_job_id=UUID(job.parent_job_id) if job.parent_job_id else None,
        iteration=job.iteration,
        research=job.research,
        budget=job.budget,
        remaining_wall_time_seconds=remaining_wall_time,
        models=job.models,
        model_capabilities=job.model_capabilities,
        report_type=job.report_type,
        report_formats=job.report_formats,
    )
