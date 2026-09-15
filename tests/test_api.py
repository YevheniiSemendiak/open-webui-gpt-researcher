from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock
from urllib.parse import urlsplit

import httpx


async def test_job_lifecycle_and_artifact_download(
    api_client: tuple[httpx.AsyncClient, Any],
    service_headers: dict[str, str],
    job_payload: dict[str, object],
) -> None:
    client, app = api_client
    response = await client.post("/v1/research-jobs", headers=service_headers, json=job_payload)
    assert response.status_code == 202, response.text
    job_id = response.json()["id"]

    duplicate = await client.post("/v1/research-jobs", headers=service_headers, json=job_payload)
    assert duplicate.status_code == 200
    assert duplicate.json()["id"] == job_id

    async with app.state.database.session() as session, session.begin():
        claim = await app.state.repository.claim_next(session, max_concurrent_jobs=5)
    assert claim is not None
    runner_headers = {"Authorization": f"Bearer {claim.runner_token}"}

    started = await client.post(f"/internal/jobs/{job_id}/started", headers=runner_headers)
    assert started.json() == {"state": "running"}
    progress = await client.post(
        f"/internal/jobs/{job_id}/events",
        headers=runner_headers,
        json={"event_type": "research.progress", "data": {"stage": "writing"}},
    )
    assert progress.status_code == 200

    completed = await client.post(
        f"/internal/jobs/{job_id}/complete",
        headers=runner_headers,
        json={
            "report_markdown": "# Report\n\nA useful result.",
            "research_notes_markdown": "# Notes",
            "sources": [{"url": "https://example.com"}],
            "usage": {"input_tokens": 100, "output_tokens": 50},
        },
    )
    assert completed.json() == {"state": "succeeded"}

    job = await client.get(f"/v1/research-jobs/{job_id}", headers=service_headers)
    assert job.json()["state"] == "succeeded"
    report_url = job.json()["result"]["artifacts"]["report.md"]
    parsed = urlsplit(report_url)
    artifact = await client.get(f"{parsed.path}?{parsed.query}")
    assert artifact.status_code == 200
    assert artifact.text.startswith("# Report")
    assert artifact.headers["content-disposition"] == 'attachment; filename="report.md"'


async def test_idempotency_conflict_and_authentication(
    api_client: tuple[httpx.AsyncClient, Any],
    service_headers: dict[str, str],
    job_payload: dict[str, object],
) -> None:
    client, _ = api_client
    assert (await client.post("/v1/research-jobs", json=job_payload)).status_code == 422
    assert (
        await client.post(
            "/v1/research-jobs",
            headers={**service_headers, "X-Research-Service-Token": "wrong"},
            json=job_payload,
        )
    ).status_code == 401
    assert (
        await client.post("/v1/research-jobs", headers=service_headers, json=job_payload)
    ).status_code == 202
    changed = {**job_payload, "query": "A different research question"}
    conflict = await client.post("/v1/research-jobs", headers=service_headers, json=changed)
    assert conflict.status_code == 409


async def test_pending_job_can_be_cancelled_immediately(
    api_client: tuple[httpx.AsyncClient, Any],
    service_headers: dict[str, str],
    job_payload: dict[str, object],
) -> None:
    client, _ = api_client
    created = await client.post("/v1/research-jobs", headers=service_headers, json=job_payload)
    job_id = created.json()["id"]
    cancelled = await client.post(f"/v1/research-jobs/{job_id}:cancel", headers=service_headers)
    assert cancelled.status_code == 200
    assert cancelled.json()["state"] == "cancelled"


async def test_budget_and_ownership_are_enforced(
    api_client: tuple[httpx.AsyncClient, Any],
    service_headers: dict[str, str],
    job_payload: dict[str, object],
) -> None:
    client, _ = api_client
    too_large = dict(job_payload)
    too_large["budget"] = {
        "max_input_tokens": 500_000,
        "max_output_tokens": 2_000,
        "max_searches": 5,
        "max_wall_time_seconds": 300,
    }
    rejected = await client.post("/v1/research-jobs", headers=service_headers, json=too_large)
    assert rejected.status_code == 422

    created = await client.post("/v1/research-jobs", headers=service_headers, json=job_payload)
    job_id = created.json()["id"]
    other_user = {**service_headers, "X-OpenWebUI-User-Id": "user-2"}
    assert (await client.get(f"/v1/research-jobs/{job_id}", headers=other_user)).status_code == 404


async def test_runner_failure_events_and_private_search_budget(
    api_client: tuple[httpx.AsyncClient, Any],
    service_headers: dict[str, str],
    job_payload: dict[str, object],
) -> None:
    client, app = api_client
    created = await client.post("/v1/research-jobs", headers=service_headers, json=job_payload)
    job_id = created.json()["id"]
    async with app.state.database.session() as session, session.begin():
        claim = await app.state.repository.claim_next(session, max_concurrent_jobs=5)
    assert claim is not None
    runner_headers = {"Authorization": f"Bearer {claim.runner_token}"}
    assert (await client.get(f"/internal/jobs/{job_id}", headers=runner_headers)).status_code == 200
    assert (
        await client.post(f"/internal/jobs/{job_id}/started", headers=runner_headers)
    ).status_code == 200
    for _ in range(5):
        search = await client.post(
            f"/internal/jobs/{job_id}/search",
            headers=runner_headers,
            json={"query": "private query"},
        )
        assert search.json() == []
    exhausted = await client.post(
        f"/internal/jobs/{job_id}/search",
        headers=runner_headers,
        json={"query": "one too many"},
    )
    assert exhausted.status_code == 429
    failed = await client.post(
        f"/internal/jobs/{job_id}/failed",
        headers=runner_headers,
        json={"error": "provider failed"},
    )
    assert failed.json() == {"state": "failed"}
    events = await client.get(f"/v1/research-jobs/{job_id}/events", headers=service_headers)
    assert "event: job.failed" in events.text


async def test_running_job_cancel_handshake(
    api_client: tuple[httpx.AsyncClient, Any],
    service_headers: dict[str, str],
    job_payload: dict[str, object],
) -> None:
    client, app = api_client
    created = await client.post("/v1/research-jobs", headers=service_headers, json=job_payload)
    job_id = created.json()["id"]
    async with app.state.database.session() as session, session.begin():
        claim = await app.state.repository.claim_next(session, max_concurrent_jobs=5)
    assert claim is not None
    runner_headers = {"Authorization": f"Bearer {claim.runner_token}"}
    cancelled = await client.post(f"/v1/research-jobs/{job_id}:cancel", headers=service_headers)
    assert cancelled.json()["state"] == "cancel_requested"
    state = await client.get(f"/internal/jobs/{job_id}/state", headers=runner_headers)
    assert state.json() == {"state": "cancel_requested"}
    acknowledged = await client.post(f"/internal/jobs/{job_id}/cancelled", headers=runner_headers)
    assert acknowledged.json() == {"state": "cancelled"}


async def test_model_and_embedding_proxies_account_usage(
    api_client: tuple[httpx.AsyncClient, Any],
    service_headers: dict[str, str],
    job_payload: dict[str, object],
) -> None:
    client, app = api_client
    created = await client.post("/v1/research-jobs", headers=service_headers, json=job_payload)
    job_id = created.json()["id"]
    async with app.state.database.session() as session, session.begin():
        claim = await app.state.repository.claim_next(session, max_concurrent_jobs=5)
    assert claim is not None
    runner_headers = {"Authorization": f"Bearer {claim.runner_token}"}
    app.state.openwebui.proxy_chat_completions = AsyncMock(
        return_value=httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "answer"}}],
                "usage": {"prompt_tokens": 40, "completion_tokens": 10},
            },
        )
    )
    app.state.openwebui.proxy_embeddings = AsyncMock(
        return_value=httpx.Response(200, json={"data": [{"embedding": [0.1]}]})
    )
    completion = await client.post(
        f"/internal/jobs/{job_id}/openai/v1/chat/completions",
        headers=runner_headers,
        json={"model": "test-model", "messages": [], "max_tokens": 5_000},
    )
    assert completion.status_code == 200
    embeddings = await client.post(
        f"/internal/jobs/{job_id}/openai/v1/embeddings",
        headers=runner_headers,
        json={"model": "ignored", "input": "text"},
    )
    assert embeddings.status_code == 200
    async with app.state.database.session() as session:
        job = await app.state.repository.get_for_runner(
            session, job_id=claim.id, runner_token=claim.runner_token
        )
        assert job.usage["input_tokens"] == 40
        assert job.usage["output_tokens"] == 10


async def test_explicit_save_to_new_knowledge(
    api_client: tuple[httpx.AsyncClient, Any],
    service_headers: dict[str, str],
    job_payload: dict[str, object],
) -> None:
    client, app = api_client
    created = await client.post("/v1/research-jobs", headers=service_headers, json=job_payload)
    job_id = created.json()["id"]
    async with app.state.database.session() as session, session.begin():
        claim = await app.state.repository.claim_next(session, max_concurrent_jobs=5)
    assert claim is not None
    runner_headers = {"Authorization": f"Bearer {claim.runner_token}"}
    await client.post(f"/internal/jobs/{job_id}/started", headers=runner_headers)
    await client.post(
        f"/internal/jobs/{job_id}/complete",
        headers=runner_headers,
        json={"report_markdown": "# Save me", "sources": [], "usage": {}},
    )
    app.state.openwebui.upload_markdown = AsyncMock(return_value="file-id")
    app.state.openwebui.create_knowledge = AsyncMock(return_value="knowledge-id")
    app.state.openwebui.add_file_to_knowledge = AsyncMock()
    saved = await client.post(
        f"/v1/research-jobs/{job_id}:save-to-knowledge",
        headers=service_headers,
        json={"name": "Saved research"},
    )
    assert saved.json() == {"knowledge_id": "knowledge-id", "file_id": "file-id"}


async def test_health_and_invalid_artifact_token(
    api_client: tuple[httpx.AsyncClient, Any],
) -> None:
    client, _ = api_client
    assert (await client.get("/health/live")).json() == {"status": "ok"}
    assert (await client.get("/health/ready")).json() == {"status": "ok"}
    invalid = await client.get(
        "/v1/research-jobs/00000000-0000-0000-0000-000000000000/artifacts/report.md?token=invalid"
    )
    assert invalid.status_code == 401
