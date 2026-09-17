from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from .domain import RunnerCompletion, RunnerJobSpec

ProgressCallback = Callable[[str, dict[str, object]], Awaitable[None]]

REPORT_LANGUAGE_POLICY = (
    "language explicitly requested in the Current user research request; if that request "
    "does not explicitly name a language, use the language in which the Current user "
    "research request is written. Ignore Integration instructions when determining the "
    "report language"
)
_original_zendriver_config: Callable[..., Any] | None = None
_zendriver_proxy_url: str | None = None
_original_get_retrievers: Callable[..., list[type[Any]]] | None = None
_job_retrievers: tuple[type[Any], ...] = ()


class GatewaySearxRetriever:
    """SearXNG retriever routed through the control plane for budget accounting."""

    requires_scraping = True

    def __init__(
        self,
        query: str,
        headers: dict[str, str] | None = None,
        query_domains: list[str] | None = None,
        **_: object,
    ) -> None:
        del headers
        self.query = query
        self.query_domains = query_domains or []

    def search(self, max_results: int = 5) -> list[dict[str, object]]:
        base_url = os.environ["INTERNAL_BASE_URL"].rstrip("/")
        job_id = os.environ["JOB_ID"]
        token = os.environ["RUNNER_TOKEN"]
        response = httpx.post(
            f"{base_url}/internal/jobs/{job_id}/public-search",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "query": self.query,
                "max_results": min(max_results, 10),
                "domains": self.query_domains,
            },
            timeout=60,
        )
        response.raise_for_status()
        payload = response.json()
        return payload if isinstance(payload, list) else []


def bound_scraped_results(
    results: list[dict[str, Any]], *, per_source_chars: int, total_chars: int
) -> list[dict[str, Any]]:
    """Bound untrusted page text before GPT Researcher can put it in an LLM prompt."""
    bounded: list[dict[str, Any]] = []
    remaining = total_chars
    for item in results:
        if not isinstance(item, dict) or remaining <= 0:
            continue
        copied = dict(item)
        for key in ("raw_content", "content", "body"):
            value = copied.get(key)
            if isinstance(value, str):
                value = value[: min(per_source_chars, remaining)]
                copied[key] = value
                remaining -= len(value)
                break
        bounded.append(copied)
    return bounded


def ensure_search_queries(queries: list[dict[str, str]], query: str) -> list[dict[str, str]]:
    """Keep upstream deep research progressing when an LLM response parses empty."""
    if queries:
        return queries
    fallback = " ".join(query.split())[:500]
    if not fallback:
        return []
    return [
        {
            "query": fallback,
            "researchGoal": "Find authoritative evidence addressing the research question.",
        }
    ]


def continuation_query(spec: RunnerJobSpec) -> str:
    if spec.iteration <= 1:
        return f"<current_user_research_request>\n{spec.query}\n</current_user_research_request>"
    return (
        f"<current_user_research_request>\n{spec.query}\n</current_user_research_request>\n\n"
        "<integration_instructions>\n"
        f"This is iteration {spec.iteration} of an ongoing research thread. Use the supplied "
        "Open WebUI conversation context and prior reports as evidence. Extend, correct, and "
        "verify earlier findings instead of restarting the investigation. Produce a complete "
        "revised report and include a section titled 'Changes since previous iteration' that "
        "summarizes new evidence, corrected conclusions, contradictions, and remaining gaps.\n"
        "</integration_instructions>"
    )


def deduplicate_sources(sources: list[dict[str, object]]) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    seen: set[str] = set()
    for source in sources:
        metadata = source.get("metadata")
        metadata = metadata if isinstance(metadata, dict) else {}
        identity = str(source.get("url") or metadata.get("file_id") or "")
        if not identity:
            identity = hashlib.sha256(
                json.dumps(source, sort_keys=True, ensure_ascii=False, default=str).encode()
            ).hexdigest()
        if identity in seen:
            continue
        seen.add(identity)
        result.append(source)
    return result


def ensure_iteration_summary(report: str, spec: RunnerJobSpec) -> str:
    """Guarantee a visible iteration delta even when the upstream writer ignores it."""
    if spec.iteration <= 1 or "changes since previous iteration" in report.lower():
        return report
    focus = " ".join(spec.query.split())
    return (
        "## Changes since previous iteration\n\n"
        f"- **Requested refinement:** {focus}\n"
        "- **Context carried forward:** The preceding conversation, prior report, attached "
        "files, and Knowledge selections were supplied to this iteration.\n"
        "- **Result handling:** The revised report below supersedes the prior version; its "
        "source list is deduplicated by canonical URL or Open WebUI file identity.\n\n"
        f"{report}"
    )


def normalize_progress_update(update: Any) -> dict[str, object]:
    """Turn GPT Researcher's mutable progress object into durable JSON data."""
    if isinstance(update, dict):
        return {str(key): value for key, value in update.items()}
    data: dict[str, object] = {"stage": "researching"}
    for name in (
        "current_depth",
        "total_depth",
        "current_breadth",
        "total_breadth",
        "total_queries",
        "completed_queries",
        "current_query",
    ):
        value = getattr(update, name, None)
        if isinstance(value, (str, int, float, bool)):
            data[name] = value
    return data


class ProgressEmitter:
    """Serialize and deduplicate durable progress updates from concurrent callbacks."""

    def __init__(self, callback: ProgressCallback) -> None:
        self.callback = callback
        self._lock = asyncio.Lock()
        self._seen: set[str] = set()

    async def emit(self, data: dict[str, object]) -> None:
        signature = json.dumps(data, sort_keys=True, ensure_ascii=False, default=str)
        async with self._lock:
            if signature in self._seen:
                return
            self._seen.add(signature)
            await self.callback("research.progress", data)


class GPTResearcherTelemetry:
    """Translate selected upstream telemetry into stable, user-facing progress data."""

    def __init__(self, emitter: ProgressEmitter) -> None:
        self.emitter = emitter
        self.page_reads = 0

    async def send_json(self, payload: dict[str, Any]) -> None:
        if payload.get("type") != "logs":
            return
        step = str(payload.get("step") or "")
        content = str(payload.get("content") or "")
        if step == "scraping_content":
            count = self._first_integer(content)
            if count is not None:
                self.page_reads += count
                await self.emitter.emit(
                    {
                        "stage": "researching",
                        "activity": "pages_read",
                        "page_reads": self.page_reads,
                    }
                )
        elif step == "fetching_query_content":
            await self.emitter.emit({"stage": "researching", "activity": "extracting_evidence"})
        elif step == "writing_report":
            await self.emitter.emit({"stage": "writing"})
        elif step == "mcp_results":
            count = self._first_integer(content)
            if count is not None:
                await self.emitter.emit(
                    {
                        "stage": "researching",
                        "activity": "mcp_results",
                        "result_count": count,
                    }
                )

    async def on_tool_start(self, *_: object, **__: object) -> None:
        return None

    async def on_agent_action(self, *_: object, **__: object) -> None:
        return None

    async def on_research_step(self, step: str, details: dict[str, Any]) -> None:
        if step == "deep_research_initialize":
            await self.emitter.emit(
                {
                    "stage": "planning",
                    "activity": "research_plan_ready",
                    "breadth": self._integer(details.get("breadth")),
                    "depth": self._integer(details.get("depth")),
                }
            )
        elif step == "deep_research_complete":
            await self.emitter.emit(
                {
                    "stage": "researching",
                    "activity": "evidence_gathering_complete",
                    "visited_urls": self._integer(details.get("visited_urls")),
                }
            )
        elif step == "writing_report":
            await self.emitter.emit({"stage": "writing"})

    @staticmethod
    def _first_integer(content: str) -> int | None:
        match = re.search(r"\b(\d+)\b", content)
        return int(match.group(1)) if match else None

    @staticmethod
    def _integer(value: object) -> int | None:
        return value if isinstance(value, int) and not isinstance(value, bool) else None


class ResearchBatchTracker:
    """Label recursive upstream progress objects without claiming global completion."""

    def __init__(self) -> None:
        self._batches: list[Any] = []

    def normalize(self, update: Any) -> dict[str, object]:
        data = normalize_progress_update(update)
        if isinstance(update, dict):
            return data
        for index, candidate in enumerate(self._batches, start=1):
            if candidate is update:
                batch_number = index
                break
        else:
            self._batches.append(update)
            batch_number = len(self._batches)
        data["batch_number"] = batch_number
        data["batch_kind"] = "initial" if batch_number == 1 else "follow_up"
        return data


class OpenWebUIRetriever:
    """GPT Researcher retriever that queries the frozen Open WebUI source manifest."""

    requires_scraping = False

    def __init__(self, query: str, query_domains: list[str] | None = None) -> None:
        del query_domains
        self.query = query

    def search(self, max_results: int = 5) -> list[dict[str, object]]:
        base_url = os.environ["INTERNAL_BASE_URL"].rstrip("/")
        job_id = os.environ["JOB_ID"]
        token = os.environ["RUNNER_TOKEN"]
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
    retriever: str = "searx"
    scraper: str = "nodriver"
    crawler_proxy_url: str | None = None

    async def run(
        self,
        spec: RunnerJobSpec,
        *,
        private_context: list[dict[str, object]],
        progress: ProgressCallback,
    ) -> RunnerCompletion:
        self._configure_upstream(spec)
        from gpt_researcher import GPTResearcher

        pending_callbacks: set[asyncio.Future[None]] = set()
        emitter = ProgressEmitter(progress)
        telemetry = GPTResearcherTelemetry(emitter)
        batches = ResearchBatchTracker()

        def on_progress(update: Any) -> None:
            data = batches.normalize(update)
            task = asyncio.ensure_future(emitter.emit(data))
            pending_callbacks.add(task)
            task.add_done_callback(pending_callbacks.discard)

        research_query = continuation_query(spec)
        researcher = GPTResearcher(
            query=research_query,
            report_type=spec.report_type,
            verbose=True,
            websocket=telemetry,
            log_handler=telemetry,
        )
        researcher.cfg.language = REPORT_LANGUAGE_POLICY
        await emitter.emit(
            {"stage": "planning", "message": "Planning research and identifying sources"},
        )
        if not self.public_search_enabled:
            if not spec.sources and not spec.context_documents:
                raise ValueError("public search is disabled and no Open WebUI context was selected")
            researcher.retrievers = [OpenWebUIRetriever]
        else:
            if self.retriever == "searx":
                researcher.retrievers = [GatewaySearxRetriever]
            if spec.sources or spec.context_documents:
                researcher.retrievers.append(OpenWebUIRetriever)
        public_context = await researcher.conduct_research(on_progress=on_progress)
        if pending_callbacks:
            await asyncio.gather(*pending_callbacks, return_exceptions=True)
            pending_callbacks.clear()
        await emitter.emit({"stage": "writing"})
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
        report = str(await researcher.write_report(ext_context=merged_context)).strip()
        if not report or report.lower() == "none":
            raise RuntimeError("GPT Researcher returned an empty report")
        report = ensure_iteration_summary(report, spec)
        await emitter.emit(
            {"stage": "finalizing", "message": "Finalizing report and source artifacts"},
        )

        sources: list[dict[str, object]] = []
        raw_sources = researcher.get_research_sources()
        if isinstance(raw_sources, list):
            sources.extend(item for item in raw_sources if isinstance(item, dict))
        sources.extend(private_context)
        sources = deduplicate_sources(sources)
        notes = (
            public_context
            if isinstance(public_context, str)
            else json.dumps(public_context, ensure_ascii=False, indent=2, default=str)
        )
        costs = researcher.get_costs()
        return RunnerCompletion(
            report_markdown=report,
            research_notes_markdown=f"# Research notes\n\n{notes}",
            sources=sources,
            usage={"upstream_costs": costs},
        )

    def _configure_upstream(self, spec: RunnerJobSpec) -> None:
        """Apply process-local safety adaptations to the pinned upstream package."""
        global _job_retrievers, _original_get_retrievers, _original_zendriver_config
        global _zendriver_proxy_url
        _zendriver_proxy_url = self.crawler_proxy_url
        loaded = sys.modules.get("gpt_researcher")
        if loaded is not None and not hasattr(loaded, "__path__"):
            # Unit-test doubles expose only GPTResearcher, not the upstream package tree.
            return
        if self.retriever == "searx":
            import gpt_researcher.retrievers as retrievers

            # Deep-research child agents construct their own retrievers, so patch the
            # exported class as well as assigning it to the root researcher below.
            retrievers.SearxSearch = GatewaySearxRetriever

        configured_retrievers: list[type[Any]] = []
        if self.public_search_enabled and self.retriever == "searx":
            configured_retrievers.append(GatewaySearxRetriever)
        if spec.sources or spec.context_documents:
            configured_retrievers.append(OpenWebUIRetriever)
        _job_retrievers = tuple(configured_retrievers)

        import gpt_researcher.agent as agent_module

        if _original_get_retrievers is None:
            _original_get_retrievers = agent_module.get_retrievers

            def get_job_retrievers(headers: dict[str, str], cfg: Any) -> list[type[Any]]:
                if _job_retrievers:
                    return list(_job_retrievers)
                if _original_get_retrievers is None:
                    raise RuntimeError("GPT Researcher retriever factory was not initialized")
                return _original_get_retrievers(headers, cfg)

            agent_module.get_retrievers = get_job_retrievers

        if self.scraper == "nodriver":
            import zendriver
            from gpt_researcher.scraper.browser.nodriver_scraper import NoDriverScraper

            if _original_zendriver_config is None:
                _original_zendriver_config = zendriver.Config
                original_config = _original_zendriver_config

                def hardened_config(*args: Any, **kwargs: Any) -> Any:
                    # Kubernetes blocks privilege escalation, so Chromium's setuid
                    # sandbox cannot initialize. The Pod and container sandboxes remain.
                    kwargs.setdefault("sandbox", False)
                    browser_args = list(kwargs.get("browser_args") or [])
                    if _zendriver_proxy_url and not any(
                        argument.startswith("--proxy-server=") for argument in browser_args
                    ):
                        browser_args.append(f"--proxy-server={_zendriver_proxy_url}")
                    if browser_args:
                        kwargs["browser_args"] = browser_args
                    return original_config(*args, **kwargs)

                zendriver_module: Any = zendriver
                zendriver_module.Config = hardened_config

            if not hasattr(NoDriverScraper, "_owui_original_get_browser"):
                original = NoDriverScraper.get_browser.__func__
                NoDriverScraper._owui_original_get_browser = original

                async def get_headless_browser(cls: type[Any], headless: bool = True) -> Any:
                    del headless
                    return await cls._owui_original_get_browser(cls, headless=True)

                NoDriverScraper.get_browser = classmethod(get_headless_browser)
            NoDriverScraper.max_browsers = 1

        from gpt_researcher.actions import report_generation
        from gpt_researcher.skills import browser as browser_module
        from gpt_researcher.skills.deep_research import DeepResearchSkill

        if not hasattr(DeepResearchSkill, "_owui_original_generate_search_queries"):
            original_generate_search_queries = DeepResearchSkill.generate_search_queries
            DeepResearchSkill._owui_original_generate_search_queries = (
                original_generate_search_queries
            )

            async def resilient_generate_search_queries(
                self: Any, query: str, num_queries: int = 3
            ) -> list[dict[str, str]]:
                generated = await self._owui_original_generate_search_queries(
                    query, num_queries=num_queries
                )
                return ensure_search_queries(generated, query)

            DeepResearchSkill.generate_search_queries = resilient_generate_search_queries

        if not hasattr(report_generation, "_owui_original_create_chat_completion"):
            report_generation._owui_original_create_chat_completion = (
                report_generation.create_chat_completion
            )

            async def non_streaming_report_completion(*args: Any, **kwargs: Any) -> str:
                # The accounting gateway returns one JSON completion so it can
                # record provider usage atomically. Upstream report writers ask
                # LangChain for SSE chunks; preserve their string contract here.
                kwargs["stream"] = False
                result = await report_generation._owui_original_create_chat_completion(
                    *args, **kwargs
                )
                return str(result)

            report_generation.create_chat_completion = non_streaming_report_completion

        if not hasattr(browser_module, "_owui_original_scrape_urls"):
            browser_module._owui_original_scrape_urls = browser_module.scrape_urls

            async def bounded_scrape_urls(*args: Any, **kwargs: Any) -> tuple[list[Any], list[Any]]:
                results, images = await browser_module._owui_original_scrape_urls(*args, **kwargs)
                per_source = int(os.environ.get("MAX_SCRAPED_SOURCE_CHARS", "20000"))
                total = int(os.environ.get("MAX_SCRAPED_BATCH_CHARS", "80000"))
                return (
                    bound_scraped_results(results, per_source_chars=per_source, total_chars=total),
                    images,
                )

            browser_module.scrape_urls = bounded_scrape_urls
