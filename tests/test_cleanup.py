from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import func, select

from open_webui_gpt_researcher.artifacts import FilesystemArtifactStore
from open_webui_gpt_researcher.cleanup import RetentionCleaner
from open_webui_gpt_researcher.config import Settings
from open_webui_gpt_researcher.db import Database, ResearchArtifact, ResearchEvent, ResearchJob
from open_webui_gpt_researcher.domain import CreateJobRequest, JobState
from open_webui_gpt_researcher.repository import JobRepository

MODEL_REQUEST = {
    "models": {"fast": "test-model", "smart": "test-model", "strategic": "test-model"},
    "model_capabilities": [
        {"id": "test-model", "context_length": 128_000, "max_output_tokens": 32_000}
    ],
}


async def test_cleanup_removes_expired_records_artifacts_and_orphans(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'cleanup.sqlite'}")
    await database.create_all()
    repository = JobRepository()
    store = FilesystemArtifactStore(str(tmp_path / "artifacts"))
    old = datetime.now(UTC) - timedelta(days=2)

    async with database.session() as session, session.begin():
        job, _ = await repository.create_job(
            session,
            user_id="user",
            idempotency_key="cleanup-job",
            request=CreateJobRequest(
                query="Old research",
                chat_id="chat",
                message_id="message",
                **MODEL_REQUEST,
            ),
        )
        job.state = JobState.SUCCEEDED.value
        job.finished_at = old
        job_id = job.id
        event = await session.scalar(select(ResearchEvent).where(ResearchEvent.job_id == job.id))
        assert event is not None
        event.created_at = old
        stored = await store.put(
            f"shared/researcher/jobs/{job.id}/report.md", b"old", "text/markdown"
        )
        await repository.add_artifact(
            session,
            job_id=job.id,  # type: ignore[arg-type]
            name="report.md",
            object_key=stored.object_key,
            media_type="text/markdown",
            size=stored.size,
        )

    orphan = await store.put("shared/researcher/jobs/orphan/report.md", b"orphan", "text/markdown")
    old_timestamp = old.timestamp()
    os.utime(tmp_path / "artifacts" / orphan.object_key, (old_timestamp, old_timestamp))

    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'cleanup.sqlite'}",
        artifact_backend="filesystem",
        artifact_path=str(tmp_path / "artifacts"),
        artifact_prefix="shared/researcher",
        event_retention_days=1,
        artifact_retention_days=1,
        job_retention_days=1,
        orphan_grace_seconds=3_600,
    )
    result = await RetentionCleaner(
        settings=settings,
        database=database,
        repository=repository,
        artifact_store=store,
    ).run()

    assert result.artifacts == 1
    assert result.events == 1
    assert result.jobs == 1
    assert result.orphaned_objects == 1
    async with database.session() as session:
        assert await session.get(ResearchJob, job_id) is None
        assert await session.scalar(select(func.count()).select_from(ResearchEvent)) == 0
        assert await session.scalar(select(func.count()).select_from(ResearchArtifact)) == 0
    assert await store.list_objects("shared/researcher/jobs") == []
    await database.close()
