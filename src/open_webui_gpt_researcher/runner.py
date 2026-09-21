from __future__ import annotations

import asyncio
import os
from contextlib import suppress
from uuid import UUID

import httpx
import structlog

from .config import Settings
from .domain import JobState, RunnerCompletion, RunnerJobSpec
from .engines import ResearchEngine

log = structlog.get_logger()


def _consume_task_result(task: asyncio.Task[object]) -> None:
    if task.cancelled():
        return
    with suppress(asyncio.CancelledError):
        task.exception()


class RunnerClient:
    def __init__(self, *, base_url: str, job_id: UUID, token: str) -> None:
        self.job_id = job_id
        self.token = token
        self.client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {token}"},
            timeout=120.0,
        )

    async def close(self) -> None:
        await self.client.aclose()

    async def get_spec(self) -> RunnerJobSpec:
        response = await self.client.get(f"/internal/jobs/{self.job_id}")
        response.raise_for_status()
        return RunnerJobSpec.model_validate(response.json())

    async def started(self) -> None:
        response = await self.client.post(f"/internal/jobs/{self.job_id}/started")
        response.raise_for_status()

    async def heartbeat(self) -> None:
        response = await self.client.post(f"/internal/jobs/{self.job_id}/heartbeat")
        response.raise_for_status()

    async def event(self, event_type: str, data: dict[str, object]) -> None:
        response = await self.client.post(
            f"/internal/jobs/{self.job_id}/events",
            json={"event_type": event_type, "data": data},
        )
        response.raise_for_status()

    async def state(self) -> JobState:
        response = await self.client.get(f"/internal/jobs/{self.job_id}/state")
        response.raise_for_status()
        return JobState(response.json()["state"])

    async def complete(self, completion: RunnerCompletion) -> None:
        response = await self.client.post(
            f"/internal/jobs/{self.job_id}/complete",
            json=completion.model_dump(mode="json"),
        )
        response.raise_for_status()

    async def failed(self, error: str) -> None:
        response = await self.client.post(
            f"/internal/jobs/{self.job_id}/failed", json={"error": error[:20_000]}
        )
        response.raise_for_status()

    async def cancelled(self) -> None:
        response = await self.client.post(f"/internal/jobs/{self.job_id}/cancelled")
        response.raise_for_status()


class Runner:
    def __init__(self, *, settings: Settings, client: RunnerClient, engine: ResearchEngine) -> None:
        self.settings = settings
        self.client = client
        self.engine = engine

    async def run(self) -> None:
        spec = await self.client.get_spec()
        self._configure_model_gateway(spec)
        await self.client.started()
        heartbeat_task = asyncio.create_task(self._heartbeat_forever())
        await self._report_progress(
            "research.progress",
            {
                "stage": "retrieval",
                "file_sources": sum(source.kind == "file" for source in spec.sources),
                "knowledge_sources": sum(source.kind == "collection" for source in spec.sources),
                "context_items": len(spec.context_documents),
            },
        )
        private_context: list[dict[str, object]] = [
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
        ]
        research_task = asyncio.create_task(
            self.engine.run(spec, private_context=private_context, progress=self._report_progress)
        )
        cancellation_task = asyncio.create_task(self._wait_for_stop())
        try:
            done, _ = await asyncio.wait(
                {research_task, cancellation_task},
                timeout=(spec.remaining_wall_time_seconds or spec.budget.max_wall_time_seconds),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if cancellation_task in done:
                stop_state = cancellation_task.result()
                await self._cancel_research(research_task)
                if stop_state == JobState.CANCEL_REQUESTED:
                    await self.client.cancelled()
                return
            if research_task not in done:
                await self._cancel_research(research_task)
                raise TimeoutError("research exceeded its wall-time budget")
            completion = research_task.result()
            await self.client.complete(completion)
        except asyncio.CancelledError:
            with suppress(httpx.HTTPError):
                await self.client.cancelled()
            raise
        except Exception as error:
            log.exception("runner.failed", job_id=str(spec.id))
            with suppress(httpx.HTTPError):
                await self.client.failed(str(error))
            raise
        finally:
            cancellation_task.cancel()
            heartbeat_task.cancel()
            with suppress(asyncio.CancelledError):
                await cancellation_task
            with suppress(asyncio.CancelledError):
                await heartbeat_task
            await self.client.close()

    async def _cancel_research(self, task: asyncio.Task[RunnerCompletion]) -> None:
        """Request cancellation without letting stuck third-party cleanup block state updates."""
        task.cancel()
        done, _ = await asyncio.wait({task}, timeout=self.settings.runner_cancel_grace_seconds)
        if task not in done:
            log.error(
                "runner.research_cancellation_stalled",
                job_id=str(getattr(self.client, "job_id", "unknown")),
                grace_seconds=self.settings.runner_cancel_grace_seconds,
            )
            task.add_done_callback(_consume_task_result)

    async def _heartbeat_forever(self) -> None:
        while True:
            await asyncio.sleep(self.settings.runner_heartbeat_seconds)
            try:
                await self.client.heartbeat()
            except httpx.HTTPError as error:
                log.warning(
                    "runner.heartbeat_failed",
                    job_id=str(getattr(self.client, "job_id", "unknown")),
                    error=str(error),
                )

    async def _wait_for_stop(self) -> JobState:
        while True:
            try:
                state = await self.client.state()
            except httpx.HTTPError as error:
                # Cancellation polling is advisory. A transient control-plane or
                # keep-alive disconnect must not terminate otherwise healthy work.
                log.warning(
                    "runner.state_poll_failed",
                    job_id=str(getattr(self.client, "job_id", "unknown")),
                    error=str(error),
                )
                await asyncio.sleep(self.settings.runner_cancel_poll_seconds)
                continue
            if state in {JobState.CANCEL_REQUESTED, JobState.FAILED}:
                return state
            await asyncio.sleep(self.settings.runner_cancel_poll_seconds)

    async def _report_progress(self, event_type: str, data: dict[str, object]) -> None:
        """Keep progress observable without making it a research failure boundary."""
        try:
            await self.client.event(event_type, data)
        except httpx.HTTPError as error:
            log.warning(
                "runner.progress_delivery_failed",
                job_id=str(getattr(self.client, "job_id", "unknown")),
                event_type=event_type,
                error=str(error),
            )

    def _configure_model_gateway(self, spec: RunnerJobSpec) -> None:
        os.environ.update(
            {
                "DEEP_RESEARCH_BREADTH": str(spec.research.breadth),
                "DEEP_RESEARCH_DEPTH": str(spec.research.depth),
                "MAX_ITERATIONS": str(spec.research.queries_per_branch),
            }
        )
        models = spec.models
        endpoint = (
            f"{self.settings.internal_base_url.rstrip('/')}/internal/jobs/{spec.id}/openai/v1"
        )
        os.environ.update(
            {
                "OPENAI_API_KEY": self.client.token,
                "OPENAI_BASE_URL": endpoint,
                "FAST_LLM": f"openai:{models.fast}",
                "SMART_LLM": f"openai:{models.smart}",
                "STRATEGIC_LLM": f"openai:{models.strategic}",
                "EMBEDDING": f"openai:{self.settings.embedding_model}",
                # OpenWebUI embedding models accept text, not token IDs from
                # OpenAI's model-specific tokenizer.
                "EMBEDDING_KWARGS": '{"check_embedding_ctx_length":false}',
            }
        )
