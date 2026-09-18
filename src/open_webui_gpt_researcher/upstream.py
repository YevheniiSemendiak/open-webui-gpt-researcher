from __future__ import annotations

import asyncio
import sys
from collections.abc import Awaitable, Callable
from contextlib import suppress
from typing import Any, cast

import requests

from .domain import RunnerJobSpec

_original_zendriver_config: Callable[..., Any] | None = None
_zendriver_proxy_url: str | None = None
_scraper_page_timeout_seconds = 120.0
_nodriver_max_concurrency = 2
_nodriver_semaphore: asyncio.Semaphore | None = None
_nodriver_semaphore_loop: asyncio.AbstractEventLoop | None = None
_original_get_retrievers: Callable[..., list[type[Any]]] | None = None
_job_retrievers: tuple[type[Any], ...] = ()
_crawler_proxy_url: str | None = None

_BROWSER_FAILURE_MARKERS = (
    "cannot find default execution context",
    "browser failed to open page",
    "failed to connect to browser",
)


class ProxyTimeoutSession(requests.Session):
    """Requests session that sends public HTTP through the configured crawler proxy."""

    def __init__(self, proxy_url: str, timeout_seconds: float) -> None:
        super().__init__()
        self.proxies.update({"http": proxy_url, "https": proxy_url})
        self._default_timeout = (min(10.0, timeout_seconds), timeout_seconds)

    def request(  # type: ignore[override]
        self, method: str, url: str, **kwargs: Any
    ) -> requests.Response:
        kwargs.setdefault("timeout", self._default_timeout)
        return super().request(method, url, **kwargs)


def sanitize_browser_scrape_result(
    result: tuple[str, list[dict[str, Any]], str],
) -> tuple[str, list[dict[str, Any]], str]:
    """Prevent browser exception strings from being accepted as source content."""
    content, images, title = result
    normalized = content.strip().lower()
    if not title and any(marker in normalized for marker in _BROWSER_FAILURE_MARKERS):
        return "", [], ""
    return content, images, title


def _consume_background_task(task: asyncio.Future[Any]) -> None:
    """Retrieve detached cleanup exceptions so asyncio does not emit noisy warnings."""
    if task.cancelled():
        return
    with suppress(asyncio.CancelledError):
        task.exception()


async def _retire_nodriver_browsers(scraper_class: type[Any]) -> None:
    """Remove and stop the shared browser pool after a page operation stalls."""
    try:
        async with asyncio.timeout(2):
            async with scraper_class.browsers_lock:
                browsers = tuple(scraper_class.browsers)
                scraper_class.browsers.clear()
    except TimeoutError:
        browsers = tuple(scraper_class.browsers)
        scraper_class.browsers.clear()

    async def stop(browser: Any) -> None:
        browser.stopping = True
        try:
            async with asyncio.timeout(5):
                await browser.driver.stop()
        except Exception as error:
            scraper_class.logger.warning("Failed to retire stalled browser: %s", error)

    await asyncio.gather(*(stop(browser) for browser in browsers), return_exceptions=True)


async def bounded_browser_scrape(
    operation: Awaitable[tuple[str, list[dict[str, Any]], str]],
    *,
    timeout_seconds: float,
    on_timeout: Callable[[], Awaitable[None]],
) -> tuple[str, list[dict[str, Any]], str]:
    """Bound one browser source without waiting forever for cancellation cleanup."""
    task: asyncio.Future[tuple[str, list[dict[str, Any]], str]] = asyncio.ensure_future(operation)
    done, _ = await asyncio.wait({task}, timeout=timeout_seconds)
    if task in done:
        return sanitize_browser_scrape_result(task.result())

    task.cancel()
    await on_timeout()
    done, _ = await asyncio.wait({task}, timeout=5)
    if task not in done:
        task.add_done_callback(_consume_background_task)
    return "", [], ""


def _get_nodriver_semaphore() -> asyncio.Semaphore:
    """Return the process-wide page limiter for the active runner event loop."""
    global _nodriver_semaphore, _nodriver_semaphore_loop
    loop = asyncio.get_running_loop()
    if _nodriver_semaphore is None or _nodriver_semaphore_loop is not loop:
        _nodriver_semaphore = asyncio.Semaphore(_nodriver_max_concurrency)
        _nodriver_semaphore_loop = loop
    return _nodriver_semaphore


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


def _install_proxy_scraper_adapters() -> None:
    """Install proxy adapters once; wrappers remain conditional on the current proxy URL."""
    from gpt_researcher.scraper.arxiv.arxiv import ArxivScraper, _paper_id_from_link
    from gpt_researcher.scraper.scraper import Scraper

    scraper_class: Any = Scraper
    if not hasattr(scraper_class, "_owui_original_init"):
        scraper_class._owui_original_init = scraper_class.__init__

        def proxy_aware_init(self: Any, *args: Any, **kwargs: Any) -> None:
            self._owui_original_init(*args, **kwargs)
            if not _crawler_proxy_url:
                return
            session = ProxyTimeoutSession(
                _crawler_proxy_url,
                _scraper_page_timeout_seconds,
            )
            session.headers.update(self.session.headers)
            session.cookies.update(self.session.cookies)
            self.session.close()
            self.session = session

        scraper_class.__init__ = proxy_aware_init

    arxiv_class: Any = ArxivScraper
    if not hasattr(arxiv_class, "_owui_original_scrape"):
        arxiv_class._owui_original_scrape = arxiv_class.scrape

        def proxy_aware_arxiv_scrape(self: Any) -> tuple[str, list[Any], str]:
            if not _crawler_proxy_url:
                return cast(tuple[str, list[Any], str], self._owui_original_scrape())

            import arxiv  # type: ignore[import-untyped]

            paper_id = _paper_id_from_link(self.link)
            if not paper_id:
                return "", [], ""
            session = self.session or ProxyTimeoutSession(
                _crawler_proxy_url,
                _scraper_page_timeout_seconds,
            )
            client = arxiv.Client(num_retries=0)
            client._session = session
            search = arxiv.Search(id_list=[paper_id], max_results=1)
            try:
                paper = next(client.results(search))
            except StopIteration:
                return "", [], ""
            authors = ", ".join(author.name for author in (paper.authors or []))
            published = paper.published.date().isoformat() if paper.published else ""
            title = paper.title or paper_id
            content = f"Published: {published}; Author: {authors}; Content: {paper.summary or ''}"
            return content, [], title

        arxiv_class.scrape = proxy_aware_arxiv_scrape


def _configure_nodriver() -> None:
    import zendriver
    from gpt_researcher.scraper.browser.nodriver_scraper import NoDriverScraper
    from zendriver.core.connection import Transaction

    global _original_zendriver_config
    transaction_class: Any = Transaction
    if not hasattr(transaction_class, "_owui_original_call"):
        transaction_class._owui_original_call = transaction_class.__call__

        def ignore_late_response(self: Any, **response: dict[str, Any]) -> None:
            if self.done():
                return
            try:
                self._owui_original_call(**response)
            except asyncio.InvalidStateError:
                if not self.done():
                    raise

        transaction_class.__call__ = ignore_late_response

    if _original_zendriver_config is None:
        _original_zendriver_config = zendriver.Config
        original_config = _original_zendriver_config

        def hardened_config(*args: Any, **kwargs: Any) -> Any:
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

    scraper_class: Any = NoDriverScraper
    if not hasattr(scraper_class, "_owui_original_get_browser"):
        scraper_class._owui_original_get_browser = scraper_class.get_browser.__func__

        async def get_headless_browser(cls: type[Any], headless: bool = True) -> Any:
            del headless
            return await cls._owui_original_get_browser(cls, headless=True)

        scraper_class.get_browser = classmethod(get_headless_browser)
    if not hasattr(scraper_class, "_owui_original_scrape_async"):
        scraper_class._owui_original_scrape_async = scraper_class.scrape_async

        async def scrape_with_timeout(self: Any) -> tuple[str, list[dict[str, Any]], str]:
            # Upstream WorkerPools are per child researcher, so their limits add
            # together during deep research. Bound pages across the whole runner
            # process to keep Chromium's aggregate tab memory predictable.
            async with _get_nodriver_semaphore():
                result = await bounded_browser_scrape(
                    self._owui_original_scrape_async(),
                    timeout_seconds=_scraper_page_timeout_seconds,
                    on_timeout=lambda: _retire_nodriver_browsers(NoDriverScraper),
                )
            if not result[0]:
                NoDriverScraper.logger.warning(
                    "NoDriver scrape failed or timed out after %.1f seconds for %s",
                    _scraper_page_timeout_seconds,
                    self.url,
                )
            return result

        scraper_class.scrape_async = scrape_with_timeout
    scraper_class.max_browsers = 1


def configure_upstream(
    spec: RunnerJobSpec,
    *,
    public_search_enabled: bool,
    retriever: str,
    scraper: str,
    scraper_page_timeout_seconds: float,
    nodriver_max_concurrency: int,
    crawler_proxy_url: str | None,
    public_retriever: type[Any],
    private_retriever: type[Any],
) -> None:
    """Apply isolated compatibility and safety adapters to the pinned upstream package."""
    global _crawler_proxy_url, _job_retrievers, _nodriver_max_concurrency
    global _nodriver_semaphore, _original_get_retrievers
    global _scraper_page_timeout_seconds, _zendriver_proxy_url
    _crawler_proxy_url = crawler_proxy_url
    _zendriver_proxy_url = crawler_proxy_url
    _scraper_page_timeout_seconds = scraper_page_timeout_seconds
    if nodriver_max_concurrency != _nodriver_max_concurrency:
        _nodriver_semaphore = None
    _nodriver_max_concurrency = nodriver_max_concurrency

    loaded = sys.modules.get("gpt_researcher")
    if loaded is not None and not hasattr(loaded, "__path__"):
        return

    if retriever == "searx":
        import gpt_researcher.retrievers as retrievers

        retrievers.SearxSearch = public_retriever

    configured_retrievers: list[type[Any]] = []
    if public_search_enabled and retriever == "searx":
        configured_retrievers.append(public_retriever)
    if spec.sources or spec.context_documents:
        configured_retrievers.append(private_retriever)
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

    if crawler_proxy_url:
        _install_proxy_scraper_adapters()
    if scraper == "nodriver":
        _configure_nodriver()

    from gpt_researcher.actions import report_generation
    from gpt_researcher.skills.deep_research import DeepResearchSkill

    skill_class: Any = DeepResearchSkill
    if not hasattr(skill_class, "_owui_original_generate_search_queries"):
        skill_class._owui_original_generate_search_queries = skill_class.generate_search_queries

        async def resilient_generate_search_queries(
            self: Any, query: str, num_queries: int = 3
        ) -> list[dict[str, str]]:
            generated = await self._owui_original_generate_search_queries(
                query, num_queries=num_queries
            )
            return ensure_search_queries(generated, query)

        skill_class.generate_search_queries = resilient_generate_search_queries

    report_module: Any = report_generation
    if not hasattr(report_module, "_owui_original_create_chat_completion"):
        report_module._owui_original_create_chat_completion = report_module.create_chat_completion

        async def non_streaming_report_completion(*args: Any, **kwargs: Any) -> str:
            kwargs["stream"] = False
            result = await report_module._owui_original_create_chat_completion(*args, **kwargs)
            return str(result)

        report_module.create_chat_completion = non_streaming_report_completion
