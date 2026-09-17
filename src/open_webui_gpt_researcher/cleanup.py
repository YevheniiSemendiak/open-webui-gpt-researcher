from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import structlog

from .artifacts import ArtifactStore
from .config import Settings
from .db import Database
from .repository import JobRepository

log = structlog.get_logger()


@dataclass(frozen=True)
class CleanupResult:
    artifacts: int = 0
    events: int = 0
    jobs: int = 0
    orphaned_objects: int = 0


class RetentionCleaner:
    """Delete expired database records and their external artifact objects."""

    def __init__(
        self,
        *,
        settings: Settings,
        database: Database,
        repository: JobRepository,
        artifact_store: ArtifactStore,
    ) -> None:
        self.settings = settings
        self.database = database
        self.repository = repository
        self.artifact_store = artifact_store

    async def run(self) -> CleanupResult:
        now = datetime.now(UTC)
        artifacts = await self._delete_expired_artifacts(
            cutoff=now - timedelta(days=self.settings.artifact_retention_days)
        )
        async with self.database.session() as session, session.begin():
            events = await self.repository.delete_expired_events(
                session,
                cutoff=now - timedelta(days=self.settings.event_retention_days),
                limit=self.settings.cleanup_batch_size,
            )
            jobs = await self.repository.delete_expired_jobs(
                session,
                cutoff=now - timedelta(days=self.settings.job_retention_days),
                limit=self.settings.cleanup_batch_size,
            )
        orphaned_objects = await self._delete_orphaned_objects(
            cutoff=now - timedelta(seconds=self.settings.orphan_grace_seconds)
        )
        result = CleanupResult(
            artifacts=artifacts,
            events=events,
            jobs=jobs,
            orphaned_objects=orphaned_objects,
        )
        log.info("retention.cleanup_complete", **result.__dict__)
        return result

    async def _delete_expired_artifacts(self, *, cutoff: datetime) -> int:
        async with self.database.session() as session:
            artifacts = await self.repository.list_expired_artifacts(
                session,
                cutoff=cutoff,
                limit=self.settings.cleanup_batch_size,
            )
        deleted = 0
        for artifact in artifacts:
            try:
                await self.artifact_store.delete(artifact.object_key)
            except Exception:
                log.exception(
                    "retention.artifact_delete_failed",
                    artifact_id=artifact.id,
                    object_key=artifact.object_key,
                )
                continue
            async with self.database.session() as session, session.begin():
                deleted += int(
                    await self.repository.delete_artifact_record(
                        session,
                        artifact_id=artifact.id,
                    )
                )
        return deleted

    async def _delete_orphaned_objects(self, *, cutoff: datetime) -> int:
        async with self.database.session() as session:
            referenced = await self.repository.list_artifact_keys(session)
        deleted = 0
        for stored in await self.artifact_store.list_objects("jobs"):
            if deleted >= self.settings.cleanup_batch_size:
                break
            if stored.object_key in referenced or stored.last_modified >= cutoff:
                continue
            try:
                await self.artifact_store.delete(stored.object_key)
            except Exception:
                log.exception(
                    "retention.orphan_delete_failed",
                    object_key=stored.object_key,
                )
            else:
                deleted += 1
        return deleted
