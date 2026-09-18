from __future__ import annotations

from uuid import uuid4

import httpx
import pytest

from open_webui_gpt_researcher.domain import RunnerCompletion
from open_webui_gpt_researcher.runner import RunnerClient


async def test_runner_client_initializes_with_socks_proxy_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HTTP_PROXY", "socks5h://proxy:1080")
    monkeypatch.setenv("HTTPS_PROXY", "socks5h://proxy:1080")
    monkeypatch.setenv("NO_PROXY", "api,.svc,.svc.cluster.local")

    client = RunnerClient(base_url="http://api", job_id=uuid4(), token="token")
    await client.close()


async def test_runner_client_protocol() -> None:
    job_id = uuid4()
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        assert request.headers["authorization"] == "Bearer token"
        if request.url.path == f"/internal/jobs/{job_id}":
            return httpx.Response(
                200,
                json={
                    "id": str(job_id),
                    "query": "question",
                    "sources": [],
                    "budget": {},
                    "models": {
                        "fast": "test-model",
                        "smart": "test-model",
                        "strategic": "test-model",
                    },
                    "model_capabilities": [
                        {
                            "id": "test-model",
                            "context_length": 128000,
                            "max_output_tokens": 32000,
                        }
                    ],
                    "report_type": "deep",
                    "report_formats": ["markdown"],
                },
            )
        if request.url.path.endswith("/state"):
            return httpx.Response(200, json={"state": "running"})
        if request.url.path.endswith("/search"):
            return httpx.Response(200, json=[{"text": "passage"}])
        return httpx.Response(200, json={"ok": True})

    client = RunnerClient(base_url="http://api", job_id=job_id, token="token")
    await client.client.aclose()
    client.client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://api",
        headers={"Authorization": "Bearer token"},
    )
    assert (await client.get_spec()).query == "question"
    await client.started()
    await client.heartbeat()
    await client.event("progress", {})
    assert (await client.state()).value == "running"
    await client.complete(RunnerCompletion(report_markdown="# report"))
    await client.failed("failure")
    await client.cancelled()
    await client.close()
    assert len(seen) == 8
