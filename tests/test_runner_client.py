from __future__ import annotations

from uuid import uuid4

import httpx

from open_webui_gpt_researcher.domain import RunnerCompletion
from open_webui_gpt_researcher.runner import RunnerClient


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
                    "model_profile": "default",
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
    await client.event("progress", {})
    assert await client.retrieve_private_context("query") == [{"text": "passage"}]
    assert (await client.state()).value == "running"
    await client.complete(RunnerCompletion(report_markdown="# report"))
    await client.failed("failure")
    await client.cancelled()
    await client.close()
    assert len(seen) == 8
