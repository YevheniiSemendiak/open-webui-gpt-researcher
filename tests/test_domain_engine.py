from __future__ import annotations

import sys
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError

from open_webui_gpt_researcher.config import Settings
from open_webui_gpt_researcher.domain import (
    CreateJobRequest,
    ResearchBudget,
    ResearchShape,
    RunnerJobSpec,
)
from open_webui_gpt_researcher.engines import (
    REPORT_LANGUAGE_POLICY,
    GatewaySearxRetriever,
    GPTResearcherEngine,
    GPTResearcherTelemetry,
    OpenWebUIRetriever,
    ProgressEmitter,
    ResearchBatchTracker,
    bound_scraped_results,
    continuation_query,
    deduplicate_sources,
    ensure_iteration_summary,
    ensure_search_queries,
    normalize_progress_update,
)

TEST_MODELS = {"fast": "test-model", "smart": "test-model", "strategic": "test-model"}
TEST_CAPABILITIES = [{"id": "test-model", "context_length": 128_000, "max_output_tokens": 32_000}]


def test_sources_must_be_unique() -> None:
    with pytest.raises(ValidationError, match="sources must be unique"):
        CreateJobRequest(
            query="A valid question",
            chat_id="chat",
            message_id="message",
            models=TEST_MODELS,
            model_capabilities=TEST_CAPABILITIES,
            sources=[
                {"kind": "file", "id": "same"},
                {"kind": "file", "id": "same"},
            ],
        )


def test_settings_validate_budget_and_default_profile() -> None:
    settings = Settings(default_model_profiles={"small": "model-id"}, hard_max_queries=100)
    assert settings.resolve_default_models("small").smart == "model-id"
    with pytest.raises(ValueError, match="unknown default model profile"):
        settings.resolve_default_models("missing")
    with pytest.raises(ValueError, match="max_queries"):
        settings.validate_budget(ResearchBudget(max_queries=101))


def test_blank_reasoning_effort_is_unset() -> None:
    assert Settings(_env_file=None, reasoning_effort="").reasoning_effort is None
    assert Settings(_env_file=None, reasoning_effort="  ").reasoning_effort is None
    assert Settings(_env_file=None, reasoning_effort="high").reasoning_effort == "high"


def test_research_limits_reject_removed_token_budget_fields() -> None:
    with pytest.raises(ValidationError, match="max_input_tokens"):
        ResearchBudget.model_validate({"max_input_tokens": 120_000})
    with pytest.raises(ValidationError, match="max_output_tokens"):
        ResearchBudget.model_validate({"max_output_tokens": 24_000})


@pytest.mark.parametrize(
    ("strategy", "breadth", "depth", "queries_per_branch", "workers", "queries"),
    [
        ("focused", 1, 1, 2, 1, 5),
        ("balanced", 2, 2, 2, 6, 25),
        ("broad", 4, 2, 2, 12, 49),
        ("deep", 2, 3, 3, 14, 71),
        ("custom", 3, 3, 1, 21, 64),
    ],
)
def test_research_shape_estimates_upstream_query_fanout(
    strategy: str,
    breadth: int,
    depth: int,
    queries_per_branch: int,
    workers: int,
    queries: int,
) -> None:
    shape = ResearchShape(
        strategy=strategy,
        breadth=breadth,
        depth=depth,
        queries_per_branch=queries_per_branch,
    )
    assert shape.total_workers == workers
    assert shape.estimated_max_queries == queries


def test_research_shape_and_query_budget_are_validated_together() -> None:
    settings = Settings(hard_max_queries=100)
    deep = ResearchShape(strategy="deep", breadth=2, depth=3, queries_per_branch=3)
    settings.validate_research_shape(deep, ResearchBudget(max_queries=71))
    with pytest.raises(ValueError, match="may require up to 71"):
        settings.validate_research_shape(deep, ResearchBudget(max_queries=70))
    with pytest.raises(ValidationError, match="balanced research must use"):
        ResearchShape(strategy="balanced", breadth=1, depth=1, queries_per_branch=1)


def test_settings_resolve_role_specific_model_profile() -> None:
    settings = Settings(
        default_model_profiles={
            "quality": {
                "fast": "fast-id",
                "smart": "smart-id",
                "strategic": "strategic-id",
            }
        }
    )
    roles = settings.resolve_default_models("quality")
    assert (roles.fast, roles.smart, roles.strategic) == (
        "fast-id",
        "smart-id",
        "strategic-id",
    )


def test_continuation_query_and_source_deduplication() -> None:
    spec = RunnerJobSpec(
        id=uuid4(),
        query="Expand the analysis",
        sources=[],
        iteration=2,
        budget=ResearchBudget(),
        models=TEST_MODELS,
        model_capabilities=TEST_CAPABILITIES,
        report_type="deep",
        report_formats=["markdown"],
    )
    continued = continuation_query(spec)
    assert "Changes since previous iteration" in continued
    assert "<current_user_research_request>\nExpand the analysis" in continued
    assert "<integration_instructions>" in continued
    report = ensure_iteration_summary("# Revised report", spec)
    assert report.startswith("## Changes since previous iteration")
    assert "Expand the analysis" in report
    assert ensure_iteration_summary(report, spec) == report
    sources = deduplicate_sources(
        [
            {"url": "https://example.com", "title": "one"},
            {"url": "https://example.com", "title": "duplicate"},
            {"metadata": {"file_id": "file-1"}, "text": "private"},
        ]
    )
    assert len(sources) == 2


def test_settings_use_direct_environment_names(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///direct.sqlite")
    settings = Settings(_env_file=None)
    assert settings.database_url == "sqlite+aiosqlite:///direct.sqlite"


def test_settings_validate_optional_crawler_proxy() -> None:
    assert Settings(crawler_proxy_url="").crawler_proxy_url is None
    assert (
        Settings(crawler_proxy_url="socks5://external-proxy:1080").crawler_proxy_url
        == "socks5://external-proxy:1080"
    )
    with pytest.raises(ValidationError, match="must not embed credentials"):
        Settings(crawler_proxy_url="socks5://user:password@external-proxy:1080")
    with pytest.raises(ValidationError, match="must be an HTTP"):
        Settings(crawler_proxy_url="socks5h://external-proxy:1080")


async def test_gpt_researcher_adapter_merges_private_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instances: list[object] = []

    class FakeGPTResearcher:
        def __init__(self, **kwargs: object) -> None:
            self.kwargs = kwargs
            self.cfg = SimpleNamespace(language="english")
            self.retrievers: list[object] = []
            instances.append(self)

        async def conduct_research(self, on_progress: object) -> list[str]:
            on_progress({"stage": "searching"})  # type: ignore[operator]
            return ["public evidence"]

        async def write_report(self, *, ext_context: object) -> str:
            assert "private evidence" in str(ext_context)
            return "# Live report"

        def get_research_sources(self) -> list[dict[str, object]]:
            return [{"url": "https://example.com"}]

        def get_costs(self) -> float:
            return 1.5

    monkeypatch.setitem(
        sys.modules, "gpt_researcher", SimpleNamespace(GPTResearcher=FakeGPTResearcher)
    )
    events: list[tuple[str, dict[str, object]]] = []

    async def progress(event_type: str, data: dict[str, object]) -> None:
        events.append((event_type, data))

    spec = RunnerJobSpec(
        id=uuid4(),
        query="Research with private context",
        sources=[{"kind": "collection", "id": "kb"}],
        budget=ResearchBudget(),
        models=TEST_MODELS,
        model_capabilities=TEST_CAPABILITIES,
        report_type="deep",
        report_formats=["markdown"],
    )
    result = await GPTResearcherEngine().run(
        spec,
        private_context=[{"text": "private evidence"}],
        progress=progress,
    )
    assert result.report_markdown == "# Live report"
    assert result.usage == {"upstream_costs": 1.5}
    assert events[0][1]["stage"] == "planning"
    assert {event[1]["stage"] for event in events} >= {
        "planning",
        "searching",
        "writing",
        "finalizing",
    }
    assert instances[0].retrievers[0] is GatewaySearxRetriever  # type: ignore[union-attr]
    assert OpenWebUIRetriever in instances[0].retrievers  # type: ignore[union-attr]
    assert instances[0].cfg.language == REPORT_LANGUAGE_POLICY  # type: ignore[union-attr]
    assert instances[0].kwargs["query"] == (  # type: ignore[union-attr]
        "<current_user_research_request>\nResearch with private context\n"
        "</current_user_research_request>"
    )


async def test_gpt_researcher_can_use_only_openwebui_sources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instances: list[object] = []

    class FakeGPTResearcher:
        def __init__(self, **kwargs: object) -> None:
            self.cfg = SimpleNamespace(language="english")
            self.retrievers: list[object] = ["public-retriever"]
            instances.append(self)

        async def conduct_research(self, on_progress: object) -> list[str]:
            del on_progress
            return ["private evidence"]

        async def write_report(self, *, ext_context: object) -> str:
            del ext_context
            return "# Private-only report"

        def get_research_sources(self) -> list[dict[str, object]]:
            return []

        def get_costs(self) -> float:
            return 0.0

    monkeypatch.setitem(
        sys.modules, "gpt_researcher", SimpleNamespace(GPTResearcher=FakeGPTResearcher)
    )
    spec = RunnerJobSpec(
        id=uuid4(),
        query="Research private sources",
        sources=[{"kind": "collection", "id": "kb"}],
        budget=ResearchBudget(),
        models=TEST_MODELS,
        model_capabilities=TEST_CAPABILITIES,
        report_type="deep",
        report_formats=["markdown"],
    )

    async def progress(event_type: str, data: dict[str, object]) -> None:
        del event_type, data

    engine = GPTResearcherEngine(public_search_enabled=False)
    result = await engine.run(
        spec, private_context=[{"text": "private evidence"}], progress=progress
    )
    assert result.report_markdown == "# Private-only report"
    assert instances[0].retrievers == [OpenWebUIRetriever]  # type: ignore[union-attr]


async def test_empty_upstream_report_is_a_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeGPTResearcher:
        def __init__(self, **kwargs: object) -> None:
            del kwargs
            self.cfg = SimpleNamespace(language="english")
            self.retrievers: list[object] = []

        async def conduct_research(self, on_progress: object) -> list[str]:
            del on_progress
            return ["evidence"]

        async def write_report(self, *, ext_context: object) -> str:
            del ext_context
            return ""

        def get_research_sources(self) -> list[dict[str, object]]:
            return []

        def get_costs(self) -> float:
            return 0.0

    monkeypatch.setitem(
        sys.modules, "gpt_researcher", SimpleNamespace(GPTResearcher=FakeGPTResearcher)
    )
    spec = RunnerJobSpec(
        id=uuid4(),
        query="Research something",
        sources=[],
        budget=ResearchBudget(),
        models=TEST_MODELS,
        model_capabilities=TEST_CAPABILITIES,
        report_type="deep",
        report_formats=["markdown"],
    )

    async def progress(event_type: str, data: dict[str, object]) -> None:
        del event_type, data

    with pytest.raises(RuntimeError, match="empty report"):
        await GPTResearcherEngine().run(spec, private_context=[], progress=progress)


async def test_private_only_research_requires_an_openwebui_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeGPTResearcher:
        def __init__(self, **kwargs: object) -> None:
            self.cfg = SimpleNamespace(language="english")
            self.retrievers: list[object] = ["public-retriever"]

    monkeypatch.setitem(
        sys.modules, "gpt_researcher", SimpleNamespace(GPTResearcher=FakeGPTResearcher)
    )
    spec = RunnerJobSpec(
        id=uuid4(),
        query="Research without evidence",
        sources=[],
        budget=ResearchBudget(),
        models=TEST_MODELS,
        model_capabilities=TEST_CAPABILITIES,
        report_type="deep",
        report_formats=["markdown"],
    )

    async def progress(event_type: str, data: dict[str, object]) -> None:
        del event_type, data

    with pytest.raises(ValueError, match="no Open WebUI context"):
        await GPTResearcherEngine(public_search_enabled=False).run(
            spec, private_context=[], progress=progress
        )


def test_openwebui_retriever_maps_private_passages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> list[dict[str, object]]:
            return [
                {
                    "text": "private passage",
                    "metadata": {"file_id": "file-1", "name": "Private file"},
                }
            ]

    monkeypatch.setenv("INTERNAL_BASE_URL", "http://api")
    monkeypatch.setenv("JOB_ID", str(uuid4()))
    monkeypatch.setenv("RUNNER_TOKEN", "token")
    monkeypatch.setattr("httpx.post", lambda *args, **kwargs: Response())
    result = OpenWebUIRetriever("sub-query").search()
    assert result[0]["raw_content"] == "private passage"
    assert str(result[0]["url"]).startswith("openwebui://source/file-1/")


def test_gateway_searx_retriever_routes_through_accounted_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request: dict[str, object] = {}

    class Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> list[dict[str, str]]:
            return [{"url": "https://example.com", "body": "snippet"}]

    def post(url: str, **kwargs: object) -> Response:
        request["url"] = url
        request.update(kwargs)
        return Response()

    monkeypatch.setenv("INTERNAL_BASE_URL", "http://api")
    monkeypatch.setenv("JOB_ID", "job-1")
    monkeypatch.setenv("RUNNER_TOKEN", "runner-token")
    monkeypatch.setattr("httpx.post", post)
    result = GatewaySearxRetriever("topic", query_domains=["example.com"]).search(20)
    assert request["url"] == "http://api/internal/jobs/job-1/public-search"
    assert request["json"] == {
        "query": "topic",
        "max_results": 10,
        "domains": ["example.com"],
    }
    assert result[0]["body"] == "snippet"
    assert GatewaySearxRetriever.requires_scraping is True


def test_scraped_results_are_bounded_per_source_and_batch() -> None:
    results = [
        {"url": "https://one", "raw_content": "a" * 10},
        {"url": "https://two", "raw_content": "b" * 10},
        {"url": "https://three", "raw_content": "c" * 10},
    ]
    bounded = bound_scraped_results(results, per_source_chars=6, total_chars=10)
    assert [len(item["raw_content"]) for item in bounded] == [6, 4]
    assert results[0]["raw_content"] == "a" * 10


def test_empty_generated_search_queries_fall_back_to_research_question() -> None:
    assert ensure_search_queries([], "  What   is Open WebUI?  ") == [
        {
            "query": "What is Open WebUI?",
            "researchGoal": "Find authoritative evidence addressing the research question.",
        }
    ]


def test_existing_generated_search_queries_are_preserved() -> None:
    generated = [{"query": "topic", "researchGoal": "goal"}]
    assert ensure_search_queries(generated, "fallback") is generated


def test_upstream_progress_object_is_serialized_as_useful_json() -> None:
    progress = SimpleNamespace(
        current_depth=2,
        total_depth=3,
        current_breadth=1,
        total_breadth=2,
        current_query="reliable sources",
        total_queries=8,
        completed_queries=3,
    )
    assert normalize_progress_update(progress) == {
        "stage": "researching",
        "current_depth": 2,
        "total_depth": 3,
        "current_breadth": 1,
        "total_breadth": 2,
        "total_queries": 8,
        "completed_queries": 3,
        "current_query": "reliable sources",
    }


def test_recursive_progress_objects_are_labeled_as_batches() -> None:
    tracker = ResearchBatchTracker()
    initial = SimpleNamespace(total_queries=2, completed_queries=0)
    follow_up = SimpleNamespace(total_queries=2, completed_queries=1)

    assert tracker.normalize(initial)["batch_kind"] == "initial"
    assert tracker.normalize(initial)["batch_number"] == 1
    assert tracker.normalize(follow_up)["batch_kind"] == "follow_up"
    assert tracker.normalize(follow_up)["batch_number"] == 2


async def test_filtered_upstream_telemetry_is_aggregated_and_deduplicated() -> None:
    events: list[tuple[str, dict[str, object]]] = []

    async def progress(event_type: str, data: dict[str, object]) -> None:
        events.append((event_type, data))

    emitter = ProgressEmitter(progress)
    telemetry = GPTResearcherTelemetry(emitter)
    await telemetry.on_research_step(
        "deep_research_initialize", {"breadth": 2, "depth": 3, "concurrency": 2}
    )
    await telemetry.send_json(
        {"type": "logs", "step": "scraping_content", "content": "Scraped 3 pages"}
    )
    await telemetry.send_json(
        {"type": "logs", "step": "scraping_content", "content": "Scraped 2 pages"}
    )
    await telemetry.send_json(
        {"type": "report", "step": "writing_report", "content": "partial report"}
    )
    await telemetry.send_json(
        {"type": "logs", "step": "fetching_query_content", "content": "query"}
    )
    await telemetry.send_json(
        {"type": "logs", "step": "fetching_query_content", "content": "same stage"}
    )
    await telemetry.on_research_step(
        "deep_research_complete", {"visited_urls": 4, "context_length": 10}
    )

    assert [data for _, data in events] == [
        {
            "stage": "planning",
            "activity": "research_plan_ready",
            "breadth": 2,
            "depth": 3,
        },
        {"stage": "researching", "activity": "pages_read", "page_reads": 3},
        {"stage": "researching", "activity": "pages_read", "page_reads": 5},
        {"stage": "researching", "activity": "extracting_evidence"},
        {
            "stage": "researching",
            "activity": "evidence_gathering_complete",
            "visited_urls": 4,
        },
    ]


def test_upstream_is_configured_for_accounted_search_and_hardened_browser() -> None:
    import zendriver
    from gpt_researcher import retrievers
    from gpt_researcher.actions import report_generation
    from gpt_researcher.scraper.browser.nodriver_scraper import NoDriverScraper
    from gpt_researcher.skills import browser

    spec = RunnerJobSpec(
        id=uuid4(),
        query="configuration test",
        sources=[],
        budget=ResearchBudget(),
        models=TEST_MODELS,
        model_capabilities=TEST_CAPABILITIES,
        report_type="deep",
        report_formats=["markdown"],
    )
    GPTResearcherEngine(crawler_proxy_url="socks5://external-proxy:1080")._configure_upstream(spec)

    assert retrievers.SearxSearch is GatewaySearxRetriever
    assert NoDriverScraper.max_browsers == 1
    assert hasattr(browser, "_owui_original_scrape_urls")
    from gpt_researcher.skills.deep_research import DeepResearchSkill

    assert hasattr(DeepResearchSkill, "_owui_original_generate_search_queries")
    assert hasattr(report_generation, "_owui_original_create_chat_completion")
    browser_config = zendriver.Config(headless=True)
    assert browser_config.sandbox is False
    assert "--proxy-server=socks5://external-proxy:1080" in browser_config.browser_args


def test_upstream_retriever_factory_propagates_private_sources_to_children() -> None:
    import gpt_researcher.agent as agent_module

    spec = RunnerJobSpec(
        id=uuid4(),
        query="configuration test",
        sources=[{"kind": "collection", "id": "knowledge"}],
        budget=ResearchBudget(),
        models=TEST_MODELS,
        model_capabilities=TEST_CAPABILITIES,
        report_type="deep",
        report_formats=["markdown"],
    )
    GPTResearcherEngine()._configure_upstream(spec)
    assert agent_module.get_retrievers({}, SimpleNamespace()) == [
        GatewaySearxRetriever,
        OpenWebUIRetriever,
    ]

    GPTResearcherEngine(public_search_enabled=False)._configure_upstream(spec)
    assert agent_module.get_retrievers({}, SimpleNamespace()) == [OpenWebUIRetriever]


async def test_upstream_report_generation_is_forced_non_streaming(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from gpt_researcher.actions import report_generation

    spec = RunnerJobSpec(
        id=uuid4(),
        query="configuration test",
        sources=[],
        budget=ResearchBudget(),
        models=TEST_MODELS,
        model_capabilities=TEST_CAPABILITIES,
        report_type="deep",
        report_formats=["markdown"],
    )
    GPTResearcherEngine()._configure_upstream(spec)
    received: dict[str, object] = {}

    async def completion(*args: object, **kwargs: object) -> str:
        del args
        received.update(kwargs)
        return "report"

    monkeypatch.setattr(report_generation, "_owui_original_create_chat_completion", completion)
    result = await report_generation.create_chat_completion(stream=True)
    assert result == "report"
    assert received["stream"] is False
