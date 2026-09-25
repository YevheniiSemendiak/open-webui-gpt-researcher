from __future__ import annotations

import asyncio
from typing import Literal

import structlog

from .config import Settings
from .db import AdvisoryLockLease, Database
from .domain import JobState
from .executors import DispatchStatus, Executor
from .metrics import ResearchMetrics
from .repository import JobRepository

log = structlog.get_logger()


class Controller:
    """Claims durable queue entries and dispatches runners."""

    def __init__(
        self,
        *,
        settings: Settings,
        database: Database,
        repository: JobRepository,
        executor: Executor,
        metrics: ResearchMetrics,
    ) -> None:
        self.settings = settings
        self.database = database
        self.repository = repository
        self.executor = executor
        self.metrics = metrics

    async def run_forever(self) -> None:
        log.info(
            "controller.started",
            mode=self.settings.mode,
        )
        while True:
            try:
                async with self.database.advisory_lock(
                    self.settings.controller_leader_lock_id
                ) as lease:
                    if not lease.acquired:
                        self.metrics.controller_leader.set(0)
                        await asyncio.sleep(self.settings.controller_leader_retry_seconds)
                        continue
                    self.metrics.controller_leader.set(1)
                    log.info(
                        "controller.leadership_acquired",
                        backend_pid=lease.backend_pid,
                    )
                    await self._dispatch_forever(lease=lease)
            except asyncio.CancelledError:
                self.metrics.controller_leader.set(0)
                raise
            except Exception:
                self.metrics.controller_leader.set(0)
                log.exception("controller.leader_election_failed")
                await asyncio.sleep(self.settings.controller_leader_retry_seconds)

    async def _dispatch_forever(self, lease: AdvisoryLockLease | None = None) -> None:
        while True:
            if lease is not None and not await lease.is_valid():
                log.warning("controller.leadership_lost")
                return
            reconciled = await self.reconcile_expired_dispatches()
            reconciled += await self.reconcile_stale_runners()
            dispatched = await self.dispatch_one()
            if not dispatched and reconciled == 0:
                await asyncio.sleep(self.settings.controller_poll_seconds)

    async def reconcile_expired_dispatches(self) -> int:
        async with self.database.session() as session:
            dispatches = await self.repository.list_expired_dispatches(
                session, limit=self.settings.dispatch_reconcile_batch_size
            )
        reconciled = 0
        for dispatch in dispatches:
            try:
                status = await self.executor.inspect(
                    job_id=dispatch.id,
                    attempt=dispatch.attempt,
                )
            except Exception:
                log.exception(
                    "controller.dispatch_inspection_failed",
                    job_id=str(dispatch.id),
                    attempt=dispatch.attempt,
                )
                continue

            action: Literal["renew", "requeue", "fail", "cancel"]
            if dispatch.state == JobState.CANCEL_REQUESTED:
                if status == DispatchStatus.ACTIVE:
                    try:
                        await self.executor.stop(job_id=dispatch.id, attempt=dispatch.attempt)
                    except Exception:
                        log.exception(
                            "controller.cancelled_dispatch_cleanup_failed",
                            job_id=str(dispatch.id),
                            attempt=dispatch.attempt,
                        )
                        continue
                action = "cancel"
            elif status == DispatchStatus.ACTIVE:
                action = "renew"
            elif status == DispatchStatus.MISSING:
                action = "requeue"
            else:
                action = "fail"
            async with self.database.session() as session, session.begin():
                changed = await self.repository.reconcile_expired_dispatch(
                    session,
                    dispatch=dispatch,
                    action=action,
                    lease_seconds=self.settings.dispatch_lease_seconds,
                    error="runner exited before reporting its state",
                )
            if changed:
                reconciled += 1
                self.metrics.reconciliations.labels("dispatch", action).inc()
                log.info(
                    "controller.dispatch_reconciled",
                    job_id=str(dispatch.id),
                    attempt=dispatch.attempt,
                    status=status.value,
                    action=action,
                )
        return reconciled

    async def reconcile_stale_runners(self) -> int:
        async with self.database.session() as session:
            runners = await self.repository.list_stale_runners(
                session,
                stale_seconds=self.settings.runner_stale_seconds,
                limit=self.settings.dispatch_reconcile_batch_size,
            )
        reconciled = 0
        for runner in runners:
            try:
                status = await self.executor.inspect(job_id=runner.id, attempt=runner.attempt)
                if status == DispatchStatus.ACTIVE:
                    await self.executor.stop(job_id=runner.id, attempt=runner.attempt)
            except Exception:
                log.exception(
                    "controller.stale_runner_cleanup_failed",
                    job_id=str(runner.id),
                    attempt=runner.attempt,
                )
                continue

            if runner.state == JobState.CANCEL_REQUESTED:
                action: Literal["retry", "fail", "cancel"] = "cancel"
            elif runner.attempt < self.settings.runner_max_attempts:
                action = "retry"
            else:
                action = "fail"
            error = f"runner heartbeat expired; executor status was {status.value}"
            async with self.database.session() as session, session.begin():
                changed = await self.repository.reconcile_stale_runner(
                    session,
                    runner=runner,
                    action=action,
                    error=error,
                )
            if changed:
                reconciled += 1
                self.metrics.reconciliations.labels("stale_runner", action).inc()
                log.warning(
                    "controller.stale_runner_reconciled",
                    job_id=str(runner.id),
                    attempt=runner.attempt,
                    status=status.value,
                    action=action,
                )
        return reconciled

    async def dispatch_one(self) -> bool:
        async with self.database.session() as session, session.begin():
            claim = await self.repository.claim_next(
                session,
                max_concurrent_jobs=self.settings.max_concurrent_jobs,
                lease_seconds=self.settings.dispatch_lease_seconds,
            )
        if claim is None:
            return False
        try:
            await self.executor.submit(
                job_id=claim.id,
                runner_token=claim.runner_token,
                attempt=claim.attempt,
            )
        except Exception as error:
            self.metrics.dispatches.labels("failed").inc()
            log.exception("controller.dispatch_failed", job_id=str(claim.id))
            async with self.database.session() as session, session.begin():
                await self.repository.mark_failed(
                    session,
                    job_id=claim.id,
                    runner_token=claim.runner_token,
                    error=f"runner dispatch failed: {error}",
                )
        else:
            self.metrics.dispatches.labels("succeeded").inc()
            log.info("controller.dispatched", job_id=str(claim.id))
        return True
