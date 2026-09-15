from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import (
    JSON,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class ResearchJob(Base):
    __tablename__ = "research_jobs"
    __table_args__ = (
        UniqueConstraint("user_id", "idempotency_key", name="uq_job_user_idempotency"),
        Index("ix_research_jobs_state_created", "state", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    user_id: Mapped[str] = mapped_column(String(255), index=True)
    idempotency_key: Mapped[str] = mapped_column(String(255))
    request_hash: Mapped[str] = mapped_column(String(64))
    query: Mapped[str] = mapped_column(Text)
    chat_id: Mapped[str] = mapped_column(String(255))
    message_id: Mapped[str] = mapped_column(String(255))
    sources: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    budget: Mapped[dict[str, Any]] = mapped_column(JSON)
    model_profile: Mapped[str] = mapped_column(String(100))
    report_type: Mapped[str] = mapped_column(String(100))
    report_formats: Mapped[list[str]] = mapped_column(JSON, default=list)
    state: Mapped[str] = mapped_column(String(32), index=True)
    runner_token_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    usage: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    events: Mapped[list[ResearchEvent]] = relationship(
        back_populates="job", cascade="all, delete-orphan"
    )
    artifacts: Mapped[list[ResearchArtifact]] = relationship(
        back_populates="job", cascade="all, delete-orphan"
    )


class ResearchEvent(Base):
    __tablename__ = "research_events"
    __table_args__ = (
        UniqueConstraint("job_id", "sequence", name="uq_event_job_sequence"),
        Index("ix_research_events_job_sequence", "job_id", "sequence"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(
        ForeignKey("research_jobs.id", ondelete="CASCADE"), index=True
    )
    sequence: Mapped[int] = mapped_column(Integer)
    event_type: Mapped[str] = mapped_column(String(100))
    data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    job: Mapped[ResearchJob] = relationship(back_populates="events")


class ResearchArtifact(Base):
    __tablename__ = "research_artifacts"
    __table_args__ = (UniqueConstraint("job_id", "name", name="uq_artifact_job_name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(
        ForeignKey("research_jobs.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(255))
    object_key: Mapped[str] = mapped_column(String(1024))
    media_type: Mapped[str] = mapped_column(String(255))
    size: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    job: Mapped[ResearchJob] = relationship(back_populates="artifacts")


@dataclass
class AdvisoryLockLease:
    """A PostgreSQL session-level advisory lock held by one dedicated connection."""

    connection: AsyncConnection
    lock_id: int
    backend_pid: int
    acquired: bool

    async def is_valid(self) -> bool:
        if not self.acquired or self.connection.invalidated:
            return False
        current_pid = await self.connection.scalar(text("SELECT pg_backend_pid()"))
        await self.connection.commit()
        return int(current_pid) == self.backend_pid

    async def release(self) -> None:
        if not await self.is_valid():
            return
        await self.connection.scalar(
            text("SELECT pg_advisory_unlock(:lock_id)"), {"lock_id": self.lock_id}
        )
        await self.connection.commit()
        self.acquired = False


class Database:
    """Async SQLAlchemy engine/session lifecycle."""

    def __init__(self, url: str) -> None:
        self.engine: AsyncEngine = create_async_engine(url, pool_pre_ping=True)
        self.session_factory = async_sessionmaker(self.engine, expire_on_commit=False)

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        async with self.session_factory() as session:
            yield session

    @asynccontextmanager
    async def advisory_lock(self, lock_id: int) -> AsyncIterator[AdvisoryLockLease]:
        """Try to hold a PostgreSQL advisory lock until the context exits."""
        if self.engine.dialect.name != "postgresql":
            raise RuntimeError("database leader election requires PostgreSQL")
        async with self.engine.connect() as connection:
            backend_pid = await connection.scalar(text("SELECT pg_backend_pid()"))
            acquired = await connection.scalar(
                text("SELECT pg_try_advisory_lock(:lock_id)"), {"lock_id": lock_id}
            )
            await connection.commit()
            lease = AdvisoryLockLease(
                connection=connection,
                lock_id=lock_id,
                backend_pid=int(backend_pid),
                acquired=bool(acquired),
            )
            try:
                yield lease
            finally:
                if lease.acquired:
                    with suppress(Exception):
                        await lease.release()

    async def create_all(self) -> None:
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

    async def close(self) -> None:
        await self.engine.dispose()
