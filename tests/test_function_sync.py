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
        if request.url.path.endswith("/export"):
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
        if request.url.path.endswith("/create"):
            return httpx.Response(200, json={"id": "save_deep_research_to_knowledge"})
        if request.url.path.endswith("/valves"):
            return httpx.Response(200, json={"default_searches": 17})
        return httpx.Response(200, json={"is_active": True})

    settings = Settings(
        openwebui_api_key="admin-key",
        service_token="service-token",
        internal_base_url="http://research:8090",
        function_sources_path=str(tmp_path),
        public_search_enabled=False,
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
    assert "/api/v1/functions/id/deep_research/valves/update" in paths
    assert "/api/v1/functions/id/save_deep_research_to_knowledge/toggle" in paths
    assert not any(path.endswith("/sync") for path in paths)
    deep_valves = next(
        json.loads(body)
        for method, path, body in requests
        if method == "POST" and path.endswith("/deep_research/valves/update")
    )
    assert deep_valves["default_searches"] == 17
    assert deep_valves["service_token"] == "service-token"
