from __future__ import annotations

import asyncio
import json
import math
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from typing import Annotated, Any
from uuid import UUID, uuid4

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
    ModelCapability,
    ModelRoles,
    RunnerCompletion,
    RunnerEvent,
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
from .search import PublicSearchError, SearxClient

log = structlog.get_logger()
Principal = Annotated[OpenWebUIPrincipal, Depends(require_openwebui_principal)]
RunnerToken = Annotated[str, Depends(require_runner_token)]


class ErrorBody(BaseModel):
    error: str = Field(min_length=1, max_length=20_000)


class SearchBody(BaseModel):
    query: str = Field(min_length=1, max_length=50_000)


class PublicSearchBody(SearchBody):
    max_results: int = Field(default=5, ge=1, le=10)
    domains: list[str] = Field(default_factory=list, max_length=20)


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
    start_controller: bool = True,
    database: Database | None = None,
    artifact_store: ArtifactStore | None = None,
    openwebui: OpenWebUIClient | None = None,
    public_search: SearxClient | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    settings.validate_api_secrets()
    database = database or Database(settings.database_url)
    repository = JobRepository()
    artifact_store = artifact_store or make_artifact_store(settings)
    openwebui = openwebui or OpenWebUIClient(
        base_url=settings.openwebui_url,
        api_key=settings.openwebui_api_key.get_secret_value(),
        timeout=settings.openwebui_timeout_seconds,
    )
    public_search = public_search or SearxClient(settings.searx_url)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        del app
        if settings.auto_create_schema:
            await database.create_all()
        dispatcher_task: asyncio.Task[None] | None = None
        if start_controller:
            dispatcher = Controller(
                settings=settings,
                database=database,
                repository=repository,
                executor=make_executor(settings),
            )
            dispatcher_task = asyncio.create_task(
                dispatcher.run_forever(), name="research-controller"
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
    app.state.public_search = public_search
    app.state.settings = settings

    @app.get("/health/live", include_in_schema=False)
    async def live() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/health/ready", include_in_schema=False)
    async def ready() -> dict[str, str]:
        async with database.session() as session:
            await session.execute(text("SELECT 1"))
        return {"status": "ok"}

    @app.get("/v1/models")
    async def list_accessible_models(
        principal: Principal,
        authorization: Annotated[str, Header()],
    ) -> dict[str, list[dict[str, Any]]]:
        try:
            accessible = await openwebui.list_models(authorization=authorization)
            authoritative = await openwebui.list_models()
        except OpenWebUIError as error:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(error)) from error
        accessible_ids = {str(item["id"]) for item in accessible}
        resolved = [item for item in authoritative if str(item["id"]) in accessible_ids]
        log.info(
            "openwebui.model_catalog_resolved",
            user_id=principal.user_id,
            accessible_model_count=len(accessible_ids),
            authoritative_model_count=len(authoritative),
            resolved_model_count=len(resolved),
        )
        return {"data": resolved}

    @app.post("/v1/research-jobs", status_code=status.HTTP_202_ACCEPTED)
    async def create_job(
        request: CreateJobRequest,
        principal: Principal,
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=8)],
    ) -> JSONResponse:
        try:
            settings.validate_budget(request.budget)
            settings.validate_research_shape(request.research, request.budget)
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

    @app.get("/v1/research-jobs/resolve")
    async def resolve_job(
        principal: Principal,
        chat_id: Annotated[str, Query(min_length=1, max_length=255)],
        message_id: Annotated[str, Query(min_length=1, max_length=255)],
    ) -> dict[str, str]:
        try:
            async with database.session() as session:
                job = await repository.get_by_chat_message(
                    session,
                    user_id=principal.user_id,
                    chat_id=chat_id,
                    message_id=message_id,
                )
        except JobNotFoundError as error:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "job not found") from error
        return {"id": job.id, "state": job.state}

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

    @app.get("/v1/research-jobs/{job_id}/artifacts")
    async def list_job_artifacts(job_id: UUID, principal: Principal) -> list[dict[str, object]]:
        try:
            async with database.session() as session:
                await repository.get_for_user(session, job_id=job_id, user_id=principal.user_id)
                artifacts = await repository.list_artifacts(session, job_id=job_id)
        except JobNotFoundError as error:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "job not found") from error
        return [
            {"name": item.name, "media_type": item.media_type, "size": item.size}
            for item in artifacts
        ]

    @app.get("/v1/research-jobs/{job_id}/artifacts/{artifact_name}/content")
    async def fetch_artifact_for_openwebui(
        job_id: UUID, artifact_name: str, principal: Principal
    ) -> Response:
        """Return an artifact to the trusted Open WebUI action over the private network."""
        try:
            async with database.session() as session:
                await repository.get_for_user(session, job_id=job_id, user_id=principal.user_id)
                artifact = await repository.get_artifact(session, job_id=job_id, name=artifact_name)
            content = await artifact_store.get(artifact.object_key)
        except (JobNotFoundError, FileNotFoundError) as error:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "artifact not found") from error
        return Response(
            content,
            media_type=artifact.media_type,
            headers={"Content-Disposition": f'attachment; filename="{artifact.name}"'},
        )

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
            await _publish_progress(openwebui, job, {"stage": "starting"})
            return {"state": job.state}
        except (JobNotFoundError, InvalidStateError) as error:
            raise HTTPException(status.HTTP_409_CONFLICT, str(error)) from error

    @app.post("/internal/jobs/{job_id}/heartbeat")
    async def runner_heartbeat(job_id: UUID, runner_token: RunnerToken) -> dict[str, str]:
        try:
            async with database.session() as session, session.begin():
                job = await repository.heartbeat(session, job_id=job_id, runner_token=runner_token)
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
                job = await repository.get_for_runner(
                    session, job_id=job_id, runner_token=runner_token
                )
            if event.event_type == "research.progress":
                await _publish_progress(openwebui, job, event.data)
            return {"sequence": stored.sequence}
        except (JobNotFoundError, InvalidStateError) as error:
            raise HTTPException(status.HTTP_409_CONFLICT, str(error)) from error

    @app.post("/internal/jobs/{job_id}/search")
    async def runner_search(
        job_id: UUID, body: SearchBody, runner_token: RunnerToken
    ) -> list[dict[str, object]]:
        try:
            async with database.session() as session, session.begin():
                job = await repository.get_for_runner_locked(
                    session, job_id=job_id, runner_token=runner_token
                )
                _ensure_active(job)
                await repository.consume_usage(
                    session, job_id=job_id, runner_token=runner_token, private_searches=1
                )
                spec = to_runner_spec(job)
            passages = (
                await openwebui.retrieve(query=body.query, sources=spec.sources)
                if spec.sources
                else []
            )
            results: list[dict[str, object]] = [
                {"text": passage.text, "metadata": passage.metadata} for passage in passages
            ]
            results.extend(
                {
                    "text": document.text,
                    "metadata": {
                        "source": f"openwebui-chat:{document.chat_id}",
                        "file_id": document.id,
                        "name": document.title or document.id,
                        "kind": document.kind,
                    },
                }
                for document in spec.context_documents
            )
            return results
        except JobNotFoundError as error:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "job not found") from error
        except OpenWebUIError as error:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(error)) from error

    @app.post("/internal/jobs/{job_id}/public-search")
    async def runner_public_search(
        job_id: UUID, body: PublicSearchBody, runner_token: RunnerToken
    ) -> list[dict[str, str]]:
        try:
            limit_failure: ResearchJob | None = None
            async with database.session() as session, session.begin():
                job = await repository.get_for_runner_locked(
                    session, job_id=job_id, runner_token=runner_token
                )
                _ensure_active(job)
                if not settings.public_search_enabled:
                    raise HTTPException(status.HTTP_403_FORBIDDEN, "public search is disabled")
                current = int((job.usage or {}).get("searches", 0))
                if current >= int(job.budget["max_queries"]):
                    limit_failure = await repository.mark_failed(
                        session,
                        job_id=job_id,
                        runner_token=runner_token,
                        error="public search query limit exhausted",
                    )
                else:
                    await repository.consume_usage(
                        session, job_id=job_id, runner_token=runner_token, searches=1
                    )
            if limit_failure is not None:
                message = "public search query limit exhausted"
                await _publish_failure(openwebui, limit_failure, message)
                raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, message)
            return await public_search.search(
                query=body.query,
                max_results=body.max_results,
                domains=body.domains,
            )
        except JobNotFoundError as error:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "job not found") from error
        except PublicSearchError as error:
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
            async with database.session() as session, session.begin():
                job = await repository.get_for_runner_locked(
                    session, job_id=job_id, runner_token=runner_token
                )
                if job.state != JobState.RUNNING.value:
                    raise InvalidStateError(f"cannot complete job in state {job.state}")
                attempt = job.attempt

            upload_id = uuid4()
            stored = {}
            for name, (content, media_type) in artifacts.items():
                stored[name] = await artifact_store.put(
                    f"jobs/{job_id}/attempts/{attempt}/{upload_id}/{name}",
                    content,
                    media_type,
                )
            async with database.session() as session, session.begin():
                job = await repository.get_for_runner_locked(
                    session, job_id=job_id, runner_token=runner_token
                )
                if job.state != JobState.RUNNING.value:
                    raise InvalidStateError(f"cannot complete job in state {job.state}")
                links = {
                    name: url
                    for name in stored
                    if (url := _download_url(settings, job, name)) is not None
                }
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
                for name, item in stored.items():
                    await repository.add_artifact(
                        session,
                        job_id=job_id,
                        name=name,
                        object_key=item.object_key,
                        media_type=artifacts[name][1],
                        size=item.size,
                    )
            if job.state == JobState.SUCCEEDED.value:
                await _publish_completion(openwebui, job, completion.report_markdown)
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
        estimated_input = _estimate_input_tokens(payload)
        context_error: str | None = None
        context_failure: ResearchJob | None = None
        async with database.session() as session, session.begin():
            job = await repository.get_for_runner_locked(
                session, job_id=job_id, runner_token=runner_token
            )
            _ensure_active(job)
            model_roles = ModelRoles.model_validate(job.models)
            capabilities = {
                capability.id: capability
                for capability in (
                    ModelCapability.model_validate(item) for item in job.model_capabilities
                )
            }
            requested_model = str(payload.get("model") or model_roles.smart)
            capability = capabilities.get(requested_model)
            if capability is None:
                raise HTTPException(
                    status.HTTP_403_FORBIDDEN,
                    "runner attempted to use a model outside the user's frozen model selection",
                )
            model_output_capacity = (
                capability.context_length - estimated_input - settings.model_context_safety_tokens
            )
            if model_output_capacity <= 0:
                context_error = (
                    f"model context exhausted: request needs approximately {estimated_input} "
                    f"input tokens but {requested_model} has a "
                    f"{capability.context_length} token context window"
                )
                context_failure = await repository.mark_failed(
                    session,
                    job_id=job_id,
                    runner_token=runner_token,
                    error=context_error,
                )
        if context_error is not None and context_failure is not None:
            await _publish_failure(openwebui, context_failure, context_error)
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, context_error)
        request_payload = dict(payload)
        if settings.reasoning_effort is not None:
            request_payload["reasoning_effort"] = settings.reasoning_effort
        output_limit = min(
            model_output_capacity,
            capability.max_output_tokens,
        )
        found_output_limit = False
        for key in ("max_tokens", "max_completion_tokens"):
            if key in request_payload:
                request_payload[key] = min(int(request_payload[key]), output_limit)
                found_output_limit = True
        if not found_output_limit:
            request_payload["max_tokens"] = output_limit
        request_payload["stream"] = False
        try:
            response = await openwebui.proxy_chat_completions(
                payload=request_payload,
                allowed_models={
                    model_roles.fast,
                    model_roles.smart,
                    model_roles.strategic,
                },
                default_model=model_roles.smart,
            )
        except OpenWebUIError as error:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(error)) from error
        data = response.json()
        provider_usage = data.get("usage", {}) if isinstance(data, dict) else {}
        provider_usage = provider_usage if isinstance(provider_usage, dict) else {}
        reported_input = provider_usage.get("prompt_tokens", provider_usage.get("input_tokens"))
        reported_output = provider_usage.get(
            "completion_tokens", provider_usage.get("output_tokens")
        )
        input_tokens = int(reported_input) if reported_input is not None else estimated_input
        output_tokens = (
            int(reported_output) if reported_output is not None else _estimate_output_tokens(data)
        )
        estimated_usage = reported_input is None or reported_output is None
        async with database.session() as session, session.begin():
            await repository.consume_usage(
                session,
                job_id=job_id,
                runner_token=runner_token,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                estimated_token_calls=int(estimated_usage),
            )
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


def _estimate_input_tokens(payload: dict[str, Any]) -> int:
    """Conservative model-agnostic estimate used to reject oversized prompts early."""
    serialized = json.dumps(payload.get("messages", []), ensure_ascii=False, default=str)
    return max(1, math.ceil(len(serialized.encode("utf-8")) / 3) + 16)


def _estimate_output_tokens(payload: object) -> int:
    """Estimate returned tokens when a provider omits completion usage metadata."""
    if not isinstance(payload, dict):
        return 0
    choices = payload.get("choices", [])
    if not isinstance(choices, list) or not choices:
        return 0
    serialized = json.dumps(choices, ensure_ascii=False, default=str)
    return math.ceil(len(serialized.encode("utf-8")) / 3) if serialized else 0


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
) -> None:
    with suppress(OpenWebUIError, httpx.HTTPError):
        await client.emit_message_event(
            chat_id=job.chat_id,
            message_id=job.message_id,
            event_type="replace",
            data={"content": report},
        )
        await client.emit_message_event(
            chat_id=job.chat_id,
            message_id=job.message_id,
            event_type="status",
            data={
                "description": "Deep research complete",
                "done": True,
                "job_id": str(job.id),
            },
        )


def _progress_description(data: dict[str, object]) -> str:
    stage = str(data.get("stage", "researching"))
    activity = str(data.get("activity") or "")

    if stage == "starting":
        return "Starting deep research…"
    if stage == "retrieval":
        context_parts: list[str] = []
        for key, singular, plural in (
            ("knowledge_sources", "Knowledge collection", "Knowledge collections"),
            ("file_sources", "file", "files"),
            ("context_items", "conversation context item", "conversation context items"),
        ):
            count = _progress_integer(data.get(key))
            if count:
                context_parts.append(f"{count} {singular if count == 1 else plural}")
        if context_parts:
            return f"Preparing research context · {', '.join(context_parts)}"
        return "Preparing research context…"
    if stage == "planning" and activity == "research_plan_ready":
        breadth = _progress_integer(data.get("breadth"))
        depth = _progress_integer(data.get("depth"))
        details: list[str] = []
        if breadth:
            details.append(f"{breadth} searches in the initial batch")
        if depth is not None and depth > 1:
            details.append("follow-up searches enabled")
        return "Research plan ready" + (f" · {' · '.join(details)}" if details else "")
    if stage == "planning":
        return "Planning research questions and sources…"
    if stage == "writing":
        return "Writing the report from gathered evidence…"
    if stage == "finalizing":
        return "Creating report and source artifacts…"

    if activity == "pages_read":
        page_reads = _progress_integer(data.get("page_reads"))
        if page_reads is not None:
            return f"Reading sources · {page_reads} successful page reads completed"
    if activity == "extracting_evidence":
        return "Extracting relevant evidence from retrieved pages…"
    if activity == "mcp_results":
        result_count = _progress_integer(data.get("result_count"))
        if result_count is not None:
            return f"Connected knowledge tools returned {result_count} results for the latest query"
    if activity == "evidence_gathering_complete":
        visited_urls = _progress_integer(data.get("visited_urls"))
        if visited_urls is not None:
            return f"Evidence gathering complete · {visited_urls} distinct web source URLs visited"
        return "Evidence gathering complete"

    description = (
        "Researching follow-up sources"
        if data.get("batch_kind") == "follow_up"
        else "Researching sources"
    )
    completed = _progress_integer(data.get("completed_queries"))
    total = _progress_integer(data.get("total_queries"))
    if completed is not None and total is not None and total > 0:
        description = f"{description} · {completed} of {total} searches complete in this batch"
    current = data.get("current_query")
    if isinstance(current, str) and current.strip():
        query = " ".join(current.split())
        suffix = "…" if len(query) > 120 else ""
        description = f"{description} · Latest search started: {query[:120]}{suffix}"
    return description


def _progress_integer(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


async def _publish_progress(
    client: OpenWebUIClient, job: ResearchJob, data: dict[str, object]
) -> None:
    with suppress(OpenWebUIError, httpx.HTTPError):
        await client.emit_message_event(
            chat_id=job.chat_id,
            message_id=job.message_id,
            event_type="status",
            data={"description": _progress_description(data), "done": False},
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
