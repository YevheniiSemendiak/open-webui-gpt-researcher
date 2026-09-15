from __future__ import annotations

import sys
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError

from open_webui_gpt_researcher.config import Settings
from open_webui_gpt_researcher.domain import CreateJobRequest, ResearchBudget, RunnerJobSpec
from open_webui_gpt_researcher.engines import (
    GPTResearcherEngine,
    OpenWebUIRetriever,
)


def test_sources_must_be_unique() -> None:
    with pytest.raises(ValidationError, match="sources must be unique"):
        CreateJobRequest(
            query="A valid question",
            chat_id="chat",
            message_id="message",
            sources=[
                {"kind": "file", "id": "same"},
                {"kind": "file", "id": "same"},
            ],
        )


def test_settings_validate_budget_and_profile() -> None:
    settings = Settings(model_profiles={"small": "model-id"})
    assert settings.resolve_model("small") == "model-id"
    with pytest.raises(ValueError, match="unknown model profile"):
        settings.resolve_model("missing")
    with pytest.raises(ValueError, match="max_searches"):
        settings.validate_budget(ResearchBudget(max_searches=101))


def test_settings_use_direct_environment_names(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///direct.sqlite")
    settings = Settings(_env_file=None)
    assert settings.database_url == "sqlite+aiosqlite:///direct.sqlite"


async def test_gpt_researcher_adapter_merges_private_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instances: list[object] = []

    class FakeGPTResearcher:
        def __init__(self, **kwargs: object) -> None:
            self.kwargs = kwargs
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
        model_profile="default",
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
    assert events[0][1] == {"stage": "searching"}
    assert OpenWebUIRetriever in instances[0].retrievers  # type: ignore[union-attr]


async def test_gpt_researcher_can_use_only_openwebui_sources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instances: list[object] = []

    class FakeGPTResearcher:
        def __init__(self, **kwargs: object) -> None:
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
        model_profile="default",
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


async def test_private_only_research_requires_an_openwebui_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeGPTResearcher:
        def __init__(self, **kwargs: object) -> None:
            self.retrievers: list[object] = ["public-retriever"]

    monkeypatch.setitem(
        sys.modules, "gpt_researcher", SimpleNamespace(GPTResearcher=FakeGPTResearcher)
    )
    spec = RunnerJobSpec(
        id=uuid4(),
        query="Research without evidence",
        sources=[],
        budget=ResearchBudget(),
        model_profile="default",
        report_type="deep",
        report_formats=["markdown"],
    )

    async def progress(event_type: str, data: dict[str, object]) -> None:
        del event_type, data

    with pytest.raises(ValueError, match="no Open WebUI sources"):
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
