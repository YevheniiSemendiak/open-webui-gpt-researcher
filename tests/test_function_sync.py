from __future__ import annotations

import json
from pathlib import Path

import httpx

from open_webui_gpt_researcher.config import Settings
from open_webui_gpt_researcher.function_sync import FunctionSync


async def test_function_sync_creates_updates_valves_and_activates(tmp_path: Path) -> None:
    (tmp_path / "deep_research_pipe.py").write_text("class Pipe: pass\n")
    (tmp_path / "save_research_to_knowledge.py").write_text("class Action: pass\n")
    requests: list[tuple[str, str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = request.read().decode() if request.content else None
        requests.append((request.method, request.url.path, body))
        if request.url.path == "/api/v1/functions/export":
            return httpx.Response(
                200,
                json=[
                    {
                        "id": "deep_research",
                        "name": "Old name",
                        "content": "old content",
                        "is_active": True,
                    }
                ],
            )
        if request.url.path == "/api/v1/models/model":
            return httpx.Response(404, json={"detail": "not found"})
        if request.url.path.endswith("/create"):
            return httpx.Response(200, json={"id": "save_deep_research_to_knowledge"})
        if request.url.path.endswith("/valves"):
            return httpx.Response(200, json={"default_max_queries": 17})
        return httpx.Response(200, json={"is_active": True})

    settings = Settings(
        openwebui_url="http://openwebui",
        openwebui_api_key="admin-key",
        service_token="service-token",
        internal_base_url="http://research:8090",
        function_sources_path=str(tmp_path),
        public_search_enabled=False,
        default_model_profiles={"default": "gpt-4.1-mini"},
        hard_max_queries=100,
    )
    sync = FunctionSync(settings)
    await sync.client.aclose()
    sync.client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://openwebui",
    )
    try:
        changed = await sync.run()
    finally:
        await sync.close()

    paths = [path for _, path, _ in requests]
    assert "updated:deep_research" in changed
    assert "created:save_deep_research_to_knowledge" in changed
    assert "activated:save_deep_research_to_knowledge" in changed
    assert "valves:deep_research" in changed
    assert "created:model:deep_research" in changed
    assert "/api/v1/functions/id/deep_research/valves/update" in paths
    assert "/api/v1/functions/id/save_deep_research_to_knowledge/toggle" in paths
    assert "/api/v1/models/create" in paths
    assert not any(path.endswith("/sync") for path in paths)
    deep_valves = next(
        json.loads(body)
        for method, path, body in requests
        if method == "POST" and path.endswith("/deep_research/valves/update")
    )
    assert deep_valves["default_max_queries"] == 100
    assert deep_valves["max_queries_cap"] == 100
    assert deep_valves["default_research_strategy"] == "balanced"
    assert deep_valves["service_token"] == "service-token"
    assert deep_valves["default_models"] == {
        "fast": "gpt-4.1-mini",
        "smart": "gpt-4.1-mini",
        "strategic": "gpt-4.1-mini",
    }
    action_valves = next(
        json.loads(body)
        for method, path, body in requests
        if method == "POST" and path.endswith("/save_deep_research_to_knowledge/valves/update")
    )
    assert action_valves["openwebui_url"] == "http://openwebui"
    model = next(
        json.loads(body)
        for method, path, body in requests
        if method == "POST" and path == "/api/v1/models/create"
    )
    assert model["meta"]["actionIds"] == ["save_deep_research_to_knowledge"]
    assert model["access_grants"] == [
        {"principal_type": "user", "principal_id": "*", "permission": "read"}
    ]


async def test_function_sync_preserves_empty_model_access_grants() -> None:
    requests: list[tuple[str, dict[str, object]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/api/v1/models/model":
            return httpx.Response(
                200,
                json={
                    "id": "deep_research",
                    "base_model_id": None,
                    "name": "Deep Research",
                    "meta": {"description": "old", "actionIds": []},
                    "params": {},
                    "access_grants": [],
                },
            )
        if request.method == "POST":
            requests.append((request.url.path, json.loads(request.content)))
            return httpx.Response(200, json={"id": "deep_research"})
        raise AssertionError(f"unexpected request: {request.method} {request.url.path}")

    sync = FunctionSync(
        Settings(
            _env_file=None,
            openwebui_url="http://openwebui",
            openwebui_api_key="admin-key",
        )
    )
    await sync.client.aclose()
    sync.client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://openwebui",
    )
    try:
        changed: list[str] = []
        await sync._sync_deep_research_model(changed)
    finally:
        await sync.close()

    assert changed == ["updated:model:deep_research"]
    assert requests == [
        (
            "/api/v1/models/model/update",
            {
                "id": "deep_research",
                "base_model_id": None,
                "name": "Deep Research",
                "meta": {
                    "description": "Durable, multi-source deep research.",
                    "actionIds": ["save_deep_research_to_knowledge"],
                },
                "params": {},
                "access_grants": [],
                "is_active": True,
            },
        )
    ]
