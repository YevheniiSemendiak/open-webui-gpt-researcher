from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from typing import Annotated, Any
from uuid import UUID

import httpx
import structlog
from fastapi import Depends, FastAPI, Header, HTTPException, Query, status
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from .artifacts import ArtifactStore, FilesystemArtifactStore, S3ArtifactStore
from .auth import (
    OpenWebUIPrincipal,
    create_download_token,
    require_openwebui_principal,
    require_runner_token,
    verify_download_token,
)
from .config import Settings, get_settings
from .controller import Controller
from .db import Database, ResearchJob
from .domain import (
    CreateJobRequest,
    JobState,
    RunnerCompletion,
    RunnerEvent,
    SaveToKnowledgeRequest,
)
from .executors import make_executor
from .openwebui import OpenWebUIClient, OpenWebUIError
from .repository import (
    IdempotencyConflictError,
    InvalidStateError,
    JobNotFoundError,
    JobRepository,
    request_digest,
    to_event_view,
    to_job_view,
    to_runner_spec,
)

log = structlog.get_logger()
Principal = Annotated[OpenWebUIPrincipal, Depends(require_openwebui_principal)]
RunnerToken = Annotated[str, Depends(require_runner_token)]


class ErrorBody(BaseModel):
    error: str = Field(min_length=1, max_length=20_000)


class SearchBody(BaseModel):
    query: str = Field(min_length=1, max_length=50_000)


def make_artifact_store(settings: Settings) -> ArtifactStore:
    if settings.artifact_backend == "filesystem":
        return FilesystemArtifactStore(settings.artifact_path)
    return S3ArtifactStore(
        bucket=settings.s3_bucket,
        endpoint_url=settings.s3_endpoint_url,
        region=settings.s3_region,
        access_key_id=settings.s3_access_key_id.get_secret_value(),
        secret_access_key=settings.s3_secret_access_key.get_secret_value(),
    )


def create_app(
    settings: Settings | None = None,
    *,
    database: Database | None = None,
    artifact_store: ArtifactStore | None = None,
    openwebui: OpenWebUIClient | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    settings.validate_api_secrets()
    database = database or Database(settings.database_url)
    repository = JobRepository()
    artifact_store = artifact_store or make_artifact_store(settings)
    openwebui = openwebui or OpenWebUIClient(
        base_url=settings.openwebui_url, api_key=settings.openwebui_api_key.get_secret_value()
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        del app
        if settings.auto_create_schema:
            await database.create_all()
        dispatcher_task: asyncio.Task[None] | None = None
        if settings.mode == "local":
            dispatcher = Controller(
                settings=settings,
                database=database,
                repository=repository,
                executor=make_executor(settings),
            )
            dispatcher_task = asyncio.create_task(
                dispatcher.run_forever(), name="local-research-dispatcher"
            )
        try:
            yield
        finally:
            if dispatcher_task is not None:
                dispatcher_task.cancel()
                with suppress(asyncio.CancelledError):
                    await dispatcher_task
            await openwebui.close()
            await database.close()

    app = FastAPI(
        title="Open WebUI GPT Researcher",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state.database = database
    app.state.repository = repository
    app.state.artifact_store = artifact_store
    app.state.openwebui = openwebui
    app.state.settings = settings

    @app.get("/health/live", include_in_schema=False)
    async def live() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/health/ready", include_in_schema=False)
    async def ready() -> dict[str, str]:
        async with database.session() as session:
            await session.execute(text("SELECT 1"))
        return {"status": "ok"}

    @app.post("/v1/research-jobs", status_code=status.HTTP_202_ACCEPTED)
    async def create_job(
        request: CreateJobRequest,
        principal: Principal,
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=8)],
    ) -> JSONResponse:
        try:
            settings.validate_budget(request.budget)
            settings.resolve_model(request.model_profile)
        except ValueError as error:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(error)) from error
        try:
            async with database.session() as session, session.begin():
                job, created = await repository.create_job(
                    session,
                    user_id=principal.user_id,
                    idempotency_key=idempotency_key,
                    request=request,
                )
                view = to_job_view(job)
        except IdempotencyConflictError as error:
            raise HTTPException(status.HTTP_409_CONFLICT, "idempotency key reused") from error
        except IntegrityError:
            async with database.session() as session:
                try:
                    job = await repository.get_by_idempotency(
                        session,
                        user_id=principal.user_id,
                        idempotency_key=idempotency_key,
                    )
                except JobNotFoundError as error:
                    raise HTTPException(
                        status.HTTP_409_CONFLICT, "concurrent job creation conflict"
                    ) from error
                if job.request_hash != request_digest(request):
                    raise HTTPException(
                        status.HTTP_409_CONFLICT, "idempotency key reused"
                    ) from None
                view = to_job_view(job)
                created = False
        return JSONResponse(
            status_code=status.HTTP_202_ACCEPTED if created else status.HTTP_200_OK,
            content=view.model_dump(mode="json"),
        )

    @app.get("/v1/research-jobs/{job_id}")
    async def get_job(job_id: UUID, principal: Principal) -> dict[str, object]:
        try:
            async with database.session() as session:
                job = await repository.get_for_user(
                    session, job_id=job_id, user_id=principal.user_id
                )
                return to_job_view(job).model_dump(mode="json")
        except JobNotFoundError as error:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "job not found") from error

    @app.get("/v1/research-jobs/{job_id}/events")
    async def stream_events(
        job_id: UUID,
        principal: Principal,
        after: Annotated[int, Query(ge=0)] = 0,
    ) -> StreamingResponse:
        async def generate() -> AsyncIterator[str]:
            sequence = after
            while True:
                async with database.session() as session:
                    try:
                        job = await repository.get_for_user(
                            session, job_id=job_id, user_id=principal.user_id
                        )
                        events = await repository.list_events(
                            session,
                            job_id=job_id,
                            user_id=principal.user_id,
                            after=sequence,
                        )
                    except JobNotFoundError:
                        yield 'event: error\ndata: {"error":"job not found"}\n\n'
                        return
                for event in events:
                    view = to_event_view(event)
                    sequence = view.sequence
                    yield (
                        f"id: {sequence}\nevent: {view.event_type}\ndata: "
                        f"{json.dumps(view.model_dump(mode='json'))}\n\n"
                    )
                if JobState(job.state) in {
                    JobState.SUCCEEDED,
                    JobState.FAILED,
                    JobState.CANCELLED,
                }:
                    return
                yield ": keepalive\n\n"
                await asyncio.sleep(1)

        return StreamingResponse(generate(), media_type="text/event-stream")

    @app.post("/v1/research-jobs/{job_id}:cancel")
    async def cancel_job(job_id: UUID, principal: Principal) -> dict[str, object]:
        try:
            async with database.session() as session, session.begin():
                job = await repository.request_cancel(
                    session, job_id=job_id, user_id=principal.user_id
                )
                return to_job_view(job).model_dump(mode="json")
        except JobNotFoundError as error:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "job not found") from error

    @app.get("/v1/research-jobs/{job_id}/artifacts/{artifact_name}")
    async def download_artifact(
        job_id: UUID,
        artifact_name: str,
        token: str = Query(),
    ) -> Response:
        user_id = verify_download_token(
            token,
            secret=settings.signing_secret.get_secret_value(),
            job_id=str(job_id),
            artifact_name=artifact_name,
        )
        try:
            async with database.session() as session:
                await repository.get_for_user(session, job_id=job_id, user_id=user_id)
                artifact = await repository.get_artifact(session, job_id=job_id, name=artifact_name)
            content = await artifact_store.get(artifact.object_key)
        except (JobNotFoundError, FileNotFoundError) as error:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "artifact not found") from error
        return Response(
            content,
            media_type=artifact.media_type,
            headers={"Content-Disposition": f'attachment; filename="{artifact.name}"'},
        )

    @app.post("/v1/research-jobs/{job_id}:save-to-knowledge")
    async def save_to_knowledge(
        job_id: UUID,
        request: SaveToKnowledgeRequest,
        principal: Principal,
    ) -> dict[str, str]:
        try:
            async with database.session() as session:
                job = await repository.get_for_user(
                    session, job_id=job_id, user_id=principal.user_id
                )
                if job.state != JobState.SUCCEEDED.value:
                    raise HTTPException(status.HTTP_409_CONFLICT, "job is not complete")
                artifact = await repository.get_artifact(session, job_id=job_id, name="report.md")
            content = await artifact_store.get(artifact.object_key)
            file_id = await openwebui.upload_markdown(
                name=f"deep-research-{job_id}.md", content=content
            )
            if request.name:
                knowledge_id = await openwebui.create_knowledge(
                    name=request.name, owner_user_id=principal.user_id
                )
            else:
                knowledge_id = str(request.knowledge_id)
                await openwebui.assert_knowledge_write_access(
                    knowledge_id=knowledge_id, user_id=principal.user_id
                )
            await openwebui.add_file_to_knowledge(knowledge_id=knowledge_id, file_id=file_id)
            return {"knowledge_id": knowledge_id, "file_id": file_id}
        except JobNotFoundError as error:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "job not found") from error
        except OpenWebUIError as error:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(error)) from error

    @app.get("/internal/jobs/{job_id}")
    async def runner_job(job_id: UUID, runner_token: RunnerToken) -> dict[str, object]:
        try:
            async with database.session() as session:
                job = await repository.get_for_runner(
                    session, job_id=job_id, runner_token=runner_token
                )
                return to_runner_spec(job).model_dump(mode="json")
        except JobNotFoundError as error:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "job not found") from error

    @app.get("/internal/jobs/{job_id}/state")
    async def runner_job_state(job_id: UUID, runner_token: RunnerToken) -> dict[str, str]:
        job = await _runner_job(database, repository, job_id, runner_token)
        return {"state": job.state}

    @app.post("/internal/jobs/{job_id}/started")
    async def runner_started(job_id: UUID, runner_token: RunnerToken) -> dict[str, str]:
        try:
            async with database.session() as session, session.begin():
                job = await repository.mark_started(
                    session, job_id=job_id, runner_token=runner_token
                )
                return {"state": job.state}
        except (JobNotFoundError, InvalidStateError) as error:
            raise HTTPException(status.HTTP_409_CONFLICT, str(error)) from error

    @app.post("/internal/jobs/{job_id}/events")
    async def runner_event(
        job_id: UUID, event: RunnerEvent, runner_token: RunnerToken
    ) -> dict[str, int]:
        try:
            async with database.session() as session, session.begin():
                stored = await repository.append_runner_event(
                    session,
                    job_id=job_id,
                    runner_token=runner_token,
                    event_type=event.event_type,
                    data=event.data,
                )
                return {"sequence": stored.sequence}
        except (JobNotFoundError, InvalidStateError) as error:
            raise HTTPException(status.HTTP_409_CONFLICT, str(error)) from error

    @app.post("/internal/jobs/{job_id}/search")
    async def runner_search(
        job_id: UUID, body: SearchBody, runner_token: RunnerToken
    ) -> list[dict[str, object]]:
        try:
            async with database.session() as session, session.begin():
                job = await repository.get_for_runner(
                    session, job_id=job_id, runner_token=runner_token
                )
                _ensure_active(job)
                current_searches = int((job.usage or {}).get("searches", 0))
                maximum = int(job.budget["max_searches"])
                if current_searches >= maximum:
                    raise HTTPException(
                        status.HTTP_429_TOO_MANY_REQUESTS, "search budget exhausted"
                    )
                await repository.consume_usage(
                    session, job_id=job_id, runner_token=runner_token, searches=1
                )
                sources = to_runner_spec(job).sources
            passages = await openwebui.retrieve(query=body.query, sources=sources)
            return [{"text": passage.text, "metadata": passage.metadata} for passage in passages]
        except JobNotFoundError as error:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "job not found") from error
        except OpenWebUIError as error:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(error)) from error

    @app.post("/internal/jobs/{job_id}/complete")
    async def runner_complete(
        job_id: UUID, completion: RunnerCompletion, runner_token: RunnerToken
    ) -> dict[str, str]:
        artifacts = {
            "report.md": (completion.report_markdown.encode(), "text/markdown"),
            "research-notes.md": (
                completion.research_notes_markdown.encode(),
                "text/markdown",
            ),
            "sources.json": (
                json.dumps(completion.sources, indent=2, ensure_ascii=False).encode(),
                "application/json",
            ),
            "run.json": (
                json.dumps({"usage": completion.usage}, indent=2).encode(),
                "application/json",
            ),
        }
        try:
            stored = {}
            for name, (content, media_type) in artifacts.items():
                stored[name] = await artifact_store.put(
                    f"jobs/{job_id}/{name}", content, media_type
                )
            async with database.session() as session, session.begin():
                job = await repository.get_for_runner(
                    session, job_id=job_id, runner_token=runner_token
                )
                links = {
                    name: url
                    for name in stored
                    if (url := _download_url(settings, job, name)) is not None
                }
                for name, item in stored.items():
                    await repository.add_artifact(
                        session,
                        job_id=job_id,
                        name=name,
                        object_key=item.object_key,
                        media_type=artifacts[name][1],
                        size=item.size,
                    )
                result: dict[str, object] = {
                    "artifacts": links,
                    "source_count": len(completion.sources),
                }
                job = await repository.mark_completed(
                    session,
                    job_id=job_id,
                    runner_token=runner_token,
                    completion=completion,
                    result=result,
                )
            if job.state == JobState.SUCCEEDED.value:
                await _publish_completion(openwebui, job, completion.report_markdown, links)
            return {"state": job.state}
        except (JobNotFoundError, InvalidStateError) as error:
            raise HTTPException(status.HTTP_409_CONFLICT, str(error)) from error

    @app.post("/internal/jobs/{job_id}/failed")
    async def runner_failed(
        job_id: UUID, body: ErrorBody, runner_token: RunnerToken
    ) -> dict[str, str]:
        try:
            async with database.session() as session, session.begin():
                job = await repository.mark_failed(
                    session,
                    job_id=job_id,
                    runner_token=runner_token,
                    error=body.error,
                )
            await _publish_failure(openwebui, job, body.error)
            return {"state": job.state}
        except JobNotFoundError as error:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "job not found") from error

    @app.post("/internal/jobs/{job_id}/cancelled")
    async def runner_cancelled(job_id: UUID, runner_token: RunnerToken) -> dict[str, str]:
        try:
            async with database.session() as session, session.begin():
                job = await repository.mark_cancelled(
                    session, job_id=job_id, runner_token=runner_token
                )
            await _publish_cancelled(openwebui, job)
            return {"state": job.state}
        except JobNotFoundError as error:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "job not found") from error

    @app.post("/internal/jobs/{job_id}/openai/v1/chat/completions")
    async def proxy_chat_completion(
        job_id: UUID, payload: dict[str, Any], runner_token: RunnerToken
    ) -> Response:
        job = await _runner_job(database, repository, job_id, runner_token)
        _ensure_active(job)
        allowed_model = settings.resolve_model(job.model_profile)
        usage = job.usage or {}
        if int(usage.get("input_tokens", 0)) >= int(job.budget["max_input_tokens"]):
            raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "input token budget exhausted")
        remaining = int(job.budget["max_output_tokens"]) - int(usage.get("output_tokens", 0))
        if remaining <= 0:
            raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "output token budget exhausted")
        request_payload = dict(payload)
        for key in ("max_tokens", "max_completion_tokens"):
            if key in request_payload:
                request_payload[key] = min(int(request_payload[key]), remaining)
        request_payload["stream"] = False
        try:
            response = await openwebui.proxy_chat_completions(
                payload=request_payload, allowed_model=allowed_model
            )
        except OpenWebUIError as error:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(error)) from error
        data = response.json()
        provider_usage = data.get("usage", {}) if isinstance(data, dict) else {}
        input_tokens = int(
            provider_usage.get("prompt_tokens", provider_usage.get("input_tokens", 0))
        )
        output_tokens = int(
            provider_usage.get("completion_tokens", provider_usage.get("output_tokens", 0))
        )
        async with database.session() as session, session.begin():
            totals = await repository.consume_usage(
                session,
                job_id=job_id,
                runner_token=runner_token,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
            )
        if totals["input_tokens"] > int(job.budget["max_input_tokens"]) or totals[
            "output_tokens"
        ] > int(job.budget["max_output_tokens"]):
            log.warning("runner.token_budget_exceeded", job_id=str(job_id), usage=totals)
        return JSONResponse(content=data)

    @app.post("/internal/jobs/{job_id}/openai/v1/embeddings")
    async def proxy_embeddings(
        job_id: UUID, payload: dict[str, Any], runner_token: RunnerToken
    ) -> Response:
        job = await _runner_job(database, repository, job_id, runner_token)
        _ensure_active(job)
        try:
            response = await openwebui.proxy_embeddings(
                payload=payload, model=settings.embedding_model
            )
            return JSONResponse(content=response.json())
        except OpenWebUIError as error:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(error)) from error

    return app


async def _runner_job(
    database: Database, repository: JobRepository, job_id: UUID, runner_token: str
) -> ResearchJob:
    try:
        async with database.session() as session:
            return await repository.get_for_runner(
                session, job_id=job_id, runner_token=runner_token
            )
    except JobNotFoundError as error:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "job not found") from error


def _ensure_active(job: ResearchJob) -> None:
    if JobState(job.state) in {JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELLED}:
        raise HTTPException(status.HTTP_409_CONFLICT, "job is terminal")


def _download_url(settings: Settings, job: ResearchJob, name: str) -> str | None:
    if not settings.artifact_base_url:
        return None
    token = create_download_token(
        secret=settings.signing_secret.get_secret_value(),
        job_id=job.id,
        artifact_name=name,
        user_id=job.user_id,
        ttl_seconds=settings.download_token_ttl_seconds,
    )
    return (
        f"{settings.artifact_base_url.rstrip('/')}/v1/research-jobs/{job.id}/artifacts/"
        f"{name}?token={token}"
    )


async def _publish_completion(
    client: OpenWebUIClient,
    job: ResearchJob,
    report: str,
    links: dict[str, str],
) -> None:
    download_section = ""
    if links:
        downloads = "\n".join(f"- [{name}]({url})" for name, url in links.items())
        download_section = f"\n\n## Downloads\n\n{downloads}"
    content = f"{report}{download_section}\n\n<!-- deep-research-job:{job.id} -->"
    with suppress(OpenWebUIError, httpx.HTTPError):
        await client.emit_message_event(
            chat_id=job.chat_id,
            message_id=job.message_id,
            event_type="message",
            data={"content": content},
        )
        await client.emit_message_event(
            chat_id=job.chat_id,
            message_id=job.message_id,
            event_type="status",
            data={"description": "Deep research complete", "done": True},
        )


async def _publish_failure(client: OpenWebUIClient, job: ResearchJob, error: str) -> None:
    with suppress(OpenWebUIError, httpx.HTTPError):
        await client.emit_message_event(
            chat_id=job.chat_id,
            message_id=job.message_id,
            event_type="status",
            data={"description": f"Deep research failed: {error}", "done": True},
        )


async def _publish_cancelled(client: OpenWebUIClient, job: ResearchJob) -> None:
    with suppress(OpenWebUIError, httpx.HTTPError):
        await client.emit_message_event(
            chat_id=job.chat_id,
            message_id=job.message_id,
            event_type="status",
            data={"description": "Deep research cancelled", "done": True},
        )
