from __future__ import annotations

import asyncio
import hashlib
import json
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol

from .domain import RunnerCompletion, RunnerJobSpec

ProgressCallback = Callable[[str, dict[str, object]], Awaitable[None]]


class OpenWebUIRetriever:
    """GPT Researcher retriever that queries the frozen Open WebUI source manifest."""

    requires_scraping = False

    def __init__(self, query: str, query_domains: list[str] | None = None) -> None:
        del query_domains
        self.query = query

    def search(self, max_results: int = 5) -> list[dict[str, object]]:
        import httpx

        base_url = os.environ["RESEARCH_INTERNAL_BASE_URL"].rstrip("/")
        job_id = os.environ["RESEARCH_JOB_ID"]
        token = os.environ["RESEARCH_RUNNER_TOKEN"]
        response = httpx.post(
            f"{base_url}/internal/jobs/{job_id}/search",
            headers={"Authorization": f"Bearer {token}"},
            json={"query": self.query},
            timeout=120,
        )
        response.raise_for_status()
        results: list[dict[str, object]] = []
        for passage in response.json()[:max_results]:
            if not isinstance(passage, dict):
                continue
            metadata = passage.get("metadata", {})
            metadata = metadata if isinstance(metadata, dict) else {}
            source = str(metadata.get("source") or metadata.get("file_id") or "private")
            digest = hashlib.sha256(f"{source}:{self.query}".encode()).hexdigest()[:16]
            results.append(
                {
                    "url": f"openwebui://source/{source}/{digest}",
                    "raw_content": str(passage.get("text", "")),
                    "title": str(metadata.get("name") or source),
                }
            )
        return results


class ResearchEngine(Protocol):
    async def run(
        self,
        spec: RunnerJobSpec,
        *,
        private_context: list[dict[str, object]],
        progress: ProgressCallback,
    ) -> RunnerCompletion: ...


@dataclass
class GPTResearcherEngine:
    """Thin adapter around the upstream GPT Researcher Python package."""

    public_search_enabled: bool = True

    async def run(
        self,
        spec: RunnerJobSpec,
        *,
        private_context: list[dict[str, object]],
        progress: ProgressCallback,
    ) -> RunnerCompletion:
        from gpt_researcher import GPTResearcher

        pending_callbacks: set[asyncio.Future[None]] = set()

        def on_progress(update: Any) -> None:
            data = update if isinstance(update, dict) else {"message": str(update)}
            task = asyncio.ensure_future(progress("research.progress", data))
            pending_callbacks.add(task)
            task.add_done_callback(pending_callbacks.discard)

        researcher = GPTResearcher(
            query=spec.query,
            report_type=spec.report_type,
            verbose=True,
        )
        if not self.public_search_enabled:
            if not spec.sources:
                raise ValueError(
                    "public search is disabled and no Open WebUI sources were selected"
                )
            researcher.retrievers = [OpenWebUIRetriever]
        elif spec.sources:
            researcher.retrievers.append(OpenWebUIRetriever)
        public_context = await researcher.conduct_research(on_progress=on_progress)
        private_text = "\n\n".join(
            f"[Private Open WebUI source]\n{item.get('text', '')}" for item in private_context
        )
        if isinstance(public_context, list):
            merged_context: object = (
                [*public_context, private_text] if private_text else public_context
            )
        else:
            merged_context = (
                f"{public_context}\n\n{private_text}" if private_text else public_context
            )
        report = await researcher.write_report(ext_context=merged_context)
        if pending_callbacks:
            await asyncio.gather(*pending_callbacks)

        sources: list[dict[str, object]] = []
        raw_sources = researcher.get_research_sources()
        if isinstance(raw_sources, list):
            sources.extend(item for item in raw_sources if isinstance(item, dict))
        sources.extend(private_context)
        notes = (
            public_context
            if isinstance(public_context, str)
            else json.dumps(public_context, ensure_ascii=False, indent=2, default=str)
        )
        costs = researcher.get_costs()
        return RunnerCompletion(
            report_markdown=str(report),
            research_notes_markdown=f"# Research notes\n\n{notes}",
            sources=sources,
            usage={"upstream_costs": costs},
        )
