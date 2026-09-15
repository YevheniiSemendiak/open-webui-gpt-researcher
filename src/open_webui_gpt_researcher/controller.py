from __future__ import annotations

import asyncio

import structlog

from .config import Settings
from .db import AdvisoryLockLease, Database
from .executors import Executor
from .repository import JobRepository

log = structlog.get_logger()


class Controller:
    """Claims durable queue entries and dispatches isolated runners."""

    def __init__(
        self,
        *,
        settings: Settings,
        database: Database,
        repository: JobRepository,
        executor: Executor,
    ) -> None:
        self.settings = settings
        self.database = database
        self.repository = repository
        self.executor = executor

    async def run_forever(self) -> None:
        log.info(
            "controller.started",
            executor=self.settings.executor,
            leader_election=self.settings.controller_leader_election,
        )
        if self.settings.controller_leader_election == "none":
            await self._dispatch_forever()
            return

        while True:
            try:
                async with self.database.advisory_lock(
                    self.settings.controller_leader_lock_id
                ) as lease:
                    if not lease.acquired:
                        await asyncio.sleep(self.settings.controller_leader_retry_seconds)
                        continue
                    log.info(
                        "controller.leadership_acquired",
                        backend_pid=lease.backend_pid,
                    )
                    await self._dispatch_forever(lease=lease)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("controller.leader_election_failed")
                await asyncio.sleep(self.settings.controller_leader_retry_seconds)

    async def _dispatch_forever(self, lease: AdvisoryLockLease | None = None) -> None:
        while True:
            if lease is not None and not await lease.is_valid():
                log.warning("controller.leadership_lost")
                return
            dispatched = await self.dispatch_one()
            if not dispatched:
                await asyncio.sleep(self.settings.controller_poll_seconds)

    async def dispatch_one(self) -> bool:
        async with self.database.session() as session, session.begin():
            claim = await self.repository.claim_next(
                session, max_concurrent_jobs=self.settings.max_concurrent_jobs
            )
        if claim is None:
            return False
        try:
            await self.executor.submit(job_id=claim.id, runner_token=claim.runner_token)
        except Exception as error:
            log.exception("controller.dispatch_failed", job_id=str(claim.id))
            async with self.database.session() as session, session.begin():
                await self.repository.mark_failed(
                    session,
                    job_id=claim.id,
                    runner_token=claim.runner_token,
                    error=f"runner dispatch failed: {error}",
                )
        else:
            log.info("controller.dispatched", job_id=str(claim.id))
        return True
