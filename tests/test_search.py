from __future__ import annotations

from typing import Any

import httpx
import pytest

from open_webui_gpt_researcher.search import PublicSearchError, SearxClient


async def test_searx_client_normalizes_and_bounds_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    class FakeClient:
        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)

        async def __aenter__(self) -> FakeClient:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        async def get(self, url: str, **kwargs: Any) -> httpx.Response:
            captured["url"] = url
            captured.update(kwargs)
            return httpx.Response(
                200,
                request=httpx.Request("GET", url),
                json={
                    "results": [
                        {"url": "https://example.com", "title": "Example", "content": "x" * 3000},
                        {"title": "missing URL"},
                    ]
                },
            )

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    results = await SearxClient("http://searx/").search(
        query="topic", max_results=5, domains=["example.com"]
    )
    assert captured["url"] == "http://searx/search"
    assert captured["params"]["q"] == "topic (site:example.com)"
    assert len(results) == 1
    assert len(results[0]["body"]) == 2000


async def test_searx_client_wraps_transport_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeClient:
        async def __aenter__(self) -> FakeClient:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        async def get(self, *args: object, **kwargs: object) -> httpx.Response:
            raise httpx.ConnectError("offline")

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: FakeClient())
    with pytest.raises(PublicSearchError, match="SearXNG search failed"):
        await SearxClient("http://searx").search(query="topic")
