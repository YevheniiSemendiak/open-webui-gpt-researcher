from __future__ import annotations

import asyncio
import math
import os
from contextlib import suppress
from uuid import UUID

import httpx
import structlog

from .config import Settings
from .domain import JobState, RunnerCompletion, RunnerJobSpec
from .engines import ResearchEngine

log = structlog.get_logger()


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

    async def event(self, event_type: str, data: dict[str, object]) -> None:
        response = await self.client.post(
            f"/internal/jobs/{self.job_id}/events",
            json={"event_type": event_type, "data": data},
        )
        response.raise_for_status()

    async def retrieve_private_context(self, query: str) -> list[dict[str, object]]:
        response = await self.client.post(
            f"/internal/jobs/{self.job_id}/search", json={"query": query}
        )
        response.raise_for_status()
        payload = response.json()
        return payload if isinstance(payload, list) else []

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
        await self.client.event("research.progress", {"stage": "retrieval"})
        private_context = (
            await self.client.retrieve_private_context(spec.query) if spec.sources else []
        )
        research_task = asyncio.create_task(
            self.engine.run(spec, private_context=private_context, progress=self.client.event)
        )
        cancellation_task = asyncio.create_task(self._wait_for_cancellation())
        try:
            done, _ = await asyncio.wait(
                {research_task, cancellation_task},
                timeout=spec.budget.max_wall_time_seconds,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if cancellation_task in done and cancellation_task.result():
                research_task.cancel()
                with suppress(asyncio.CancelledError):
                    await research_task
                await self.client.cancelled()
                return
            if research_task not in done:
                research_task.cancel()
                with suppress(asyncio.CancelledError):
                    await research_task
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
            with suppress(asyncio.CancelledError):
                await cancellation_task
            await self.client.close()

    async def _wait_for_cancellation(self) -> bool:
        while True:
            if await self.client.state() == JobState.CANCEL_REQUESTED:
                return True
            await asyncio.sleep(self.settings.runner_cancel_poll_seconds)

    def _configure_model_gateway(self, spec: RunnerJobSpec) -> None:
        breadth = min(5, max(1, math.isqrt(spec.budget.max_searches)))
        depth = min(3, max(1, spec.budget.max_searches // breadth))
        os.environ.update(
            {
                "DEEP_RESEARCH_BREADTH": str(breadth),
                "DEEP_RESEARCH_DEPTH": str(depth),
                "MAX_ITERATIONS": str(min(5, max(1, spec.budget.max_searches // 5))),
                "FAST_TOKEN_LIMIT": str(min(6_000, spec.budget.max_output_tokens)),
                "SMART_TOKEN_LIMIT": str(min(12_000, spec.budget.max_output_tokens)),
                "STRATEGIC_TOKEN_LIMIT": str(min(8_000, spec.budget.max_output_tokens)),
            }
        )
        if self.settings.model_route != "openwebui":
            return
        model = self.settings.resolve_model(spec.model_profile)
        endpoint = (
            f"{self.settings.internal_base_url.rstrip('/')}/internal/jobs/{spec.id}/openai/v1"
        )
        os.environ.update(
            {
                "OPENAI_API_KEY": self.client.token,
                "OPENAI_BASE_URL": endpoint,
                "FAST_LLM": f"openai:{model}",
                "SMART_LLM": f"openai:{model}",
                "STRATEGIC_LLM": f"openai:{model}",
                "EMBEDDING": f"openai:{self.settings.embedding_model}",
            }
        )
