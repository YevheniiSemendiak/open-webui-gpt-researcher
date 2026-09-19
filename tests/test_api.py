from __future__ import annotations

import asyncio
import copy
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import httpx
import pytest
from asgi_lifespan import LifespanManager

from open_webui_gpt_researcher.api import _progress_description, create_app
from open_webui_gpt_researcher.config import Settings
from open_webui_gpt_researcher.controller import Controller


@pytest.mark.parametrize("mode", ["local", "k8s"])
async def test_api_embeds_dispatcher(settings: Settings, monkeypatch: Any, mode: str) -> None:
    started = asyncio.Event()
    stopped = asyncio.Event()

    async def run_forever(self: Controller) -> None:
        del self
        started.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            stopped.set()
            raise

    monkeypatch.setattr(Controller, "run_forever", run_forever)
    app = create_app(settings.model_copy(update={"mode": mode}))
    async with LifespanManager(app):
        await asyncio.wait_for(started.wait(), timeout=1)
    assert stopped.is_set()


def test_progress_descriptions_are_accurate_and_compact() -> None:
    assert _progress_description(
        {
            "stage": "retrieval",
            "knowledge_sources": 1,
            "file_sources": 2,
            "context_items": 3,
        }
    ) == (
        "Preparing research context · 1 Knowledge collection, 2 files, 3 conversation context items"
    )
    assert _progress_description(
        {
            "stage": "researching",
            "batch_kind": "follow_up",
            "completed_queries": 1,
            "total_queries": 2,
            "current_query": "  ownership   and management changes  ",
        }
    ) == (
        "Researching follow-up sources · 1 of 2 searches complete in this batch · "
        "Latest search started: ownership and management changes"
    )
    assert (
        _progress_description(
            {
                "stage": "planning",
                "activity": "research_plan_ready",
                "breadth": 2,
                "depth": 3,
            }
        )
        == "Research plan ready · 2 searches in the initial batch · follow-up searches enabled"
    )
    assert (
        _progress_description({"stage": "researching", "activity": "pages_read", "page_reads": 7})
        == "Reading sources · 7 successful page reads completed"
    )
    assert (
        _progress_description(
            {
                "stage": "researching",
                "activity": "evidence_gathering_complete",
                "visited_urls": 11,
            }
        )
        == "Evidence gathering complete · 11 distinct web source URLs visited"
    )


async def test_model_catalog_intersects_user_access_with_authoritative_metadata(
    api_client: tuple[httpx.AsyncClient, Any],
    service_headers: dict[str, str],
) -> None:
    client, app = api_client
    app.state.openwebui.list_models = AsyncMock(
        side_effect=[
            [{"id": "allowed"}, {"id": "metadata-missing-for-user"}],
            [
                {"id": "allowed", "context_length": 128_000, "max_output_tokens": 32_000},
                {"id": "forbidden", "context_length": 128_000, "max_output_tokens": 32_000},
            ],
        ]
    )
    response = await client.get(
        "/v1/models",
        headers={**service_headers, "Authorization": "Bearer initiating-user"},
    )
    assert response.status_code == 200
    assert response.json() == {
        "data": [{"id": "allowed", "context_length": 128_000, "max_output_tokens": 32_000}]
    }
    assert app.state.openwebui.list_models.await_args_list[0].kwargs == {
        "authorization": "Bearer initiating-user"
    }
    assert app.state.openwebui.list_models.await_args_list[1].kwargs == {}


async def test_job_lifecycle_and_artifact_download(
    api_client: tuple[httpx.AsyncClient, Any],
    service_headers: dict[str, str],
    job_payload: dict[str, object],
) -> None:
    client, app = api_client
    app.state.openwebui.emit_message_event = AsyncMock()
    response = await client.post("/v1/research-jobs", headers=service_headers, json=job_payload)
    assert response.status_code == 202, response.text
    job_id = response.json()["id"]

    duplicate = await client.post("/v1/research-jobs", headers=service_headers, json=job_payload)
    assert duplicate.status_code == 200
    assert duplicate.json()["id"] == job_id

    async with app.state.database.session() as session, session.begin():
        claim = await app.state.repository.claim_next(
            session, max_concurrent_jobs=5, lease_seconds=120
        )
    assert claim is not None
    runner_headers = {"Authorization": f"Bearer {claim.runner_token}"}

    started = await client.post(f"/internal/jobs/{job_id}/started", headers=runner_headers)
    assert started.json() == {"state": "running"}
    heartbeat = await client.post(f"/internal/jobs/{job_id}/heartbeat", headers=runner_headers)
    assert heartbeat.json() == {"state": "running"}
    progress = await client.post(
        f"/internal/jobs/{job_id}/events",
        headers=runner_headers,
        json={"event_type": "research.progress", "data": {"stage": "writing"}},
    )
    assert progress.status_code == 200
    progress_updates = [
        call.kwargs["data"]
        for call in app.state.openwebui.emit_message_event.await_args_list
        if call.kwargs["event_type"] == "status" and not call.kwargs["data"]["done"]
    ]
    assert progress_updates[0]["description"] == "Starting deep research…"
    assert progress_updates[-1]["description"] == "Writing the report from gathered evidence…"

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
    assert (await client.get(f"/internal/jobs/{job_id}", headers=runner_headers)).status_code == 404
    async with app.state.database.session() as session:
        stored = await app.state.repository.get_for_user(
            session, job_id=UUID(job_id), user_id="user-1"
        )
        assert stored.runner_token_hash is None
        artifacts = await app.state.repository.list_artifacts(session, job_id=UUID(job_id))
        assert len(artifacts) == 4
        assert all(
            artifact.object_key.startswith(f"test/researcher/jobs/{job_id}/")
            for artifact in artifacts
        )

    malicious_completion = {
        "report_markdown": "# Replaced",
        "research_notes_markdown": "",
        "sources": [],
        "usage": {},
    }
    for authorization in ("Bearer invalid-runner", f"Bearer {claim.runner_token}"):
        rejected = await client.post(
            f"/internal/jobs/{job_id}/complete",
            headers={"Authorization": authorization},
            json=malicious_completion,
        )
        assert rejected.status_code == 409

    job = await client.get(f"/v1/research-jobs/{job_id}", headers=service_headers)
    assert job.json()["state"] == "succeeded"
    resolved = await client.get(
        "/v1/research-jobs/resolve",
        headers=service_headers,
        params={"chat_id": "chat-1", "message_id": "message-1"},
    )
    assert resolved.json() == {"id": job_id, "state": "succeeded"}
    manifest = await client.get(f"/v1/research-jobs/{job_id}/artifacts", headers=service_headers)
    assert [item["name"] for item in manifest.json()] == [
        "report.md",
        "research-notes.md",
        "sources.json",
        "run.json",
    ]
    internal_artifact = await client.get(
        f"/v1/research-jobs/{job_id}/artifacts/report.md/content",
        headers=service_headers,
    )
    assert internal_artifact.text.startswith("# Report")
    assert "Replaced" not in internal_artifact.text
    other_user = {**service_headers, "X-OpenWebUI-User-Id": "user-2"}
    assert (
        await client.get(
            "/v1/research-jobs/resolve",
            headers=other_user,
            params={"chat_id": "chat-1", "message_id": "message-1"},
        )
    ).status_code == 404
    assert (
        await client.get(
            f"/v1/research-jobs/{job_id}/artifacts/report.md/content",
            headers=other_user,
        )
    ).status_code == 404
    assert job.json()["result"]["artifact_names"] == [
        "report.md",
        "research-notes.md",
        "sources.json",
        "run.json",
    ]
    completion_messages = [
        call.kwargs
        for call in app.state.openwebui.emit_message_event.await_args_list
        if call.kwargs["event_type"] == "replace"
    ]
    assert completion_messages[-1]["data"]["content"] == "# Report\n\nA useful result."
    final_status = [
        call.kwargs["data"]
        for call in app.state.openwebui.emit_message_event.await_args_list
        if call.kwargs["event_type"] == "status" and call.kwargs["data"]["done"]
    ][-1]
    assert final_status["job_id"] == job_id


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


async def test_new_job_in_same_chat_continues_latest_successful_job(
    api_client: tuple[httpx.AsyncClient, Any],
    service_headers: dict[str, str],
    job_payload: dict[str, object],
) -> None:
    client, app = api_client
    first = await client.post("/v1/research-jobs", headers=service_headers, json=job_payload)
    first_id = first.json()["id"]
    async with app.state.database.session() as session, session.begin():
        job = await app.state.repository.get_for_user(session, job_id=first_id, user_id="user-1")
        job.state = "succeeded"

    second_headers = {**service_headers, "Idempotency-Key": "idempotency-key-0002"}
    second_payload = {
        **job_payload,
        "message_id": "message-2",
        "query": "Expand the prior findings",
        "context_documents": [
            {
                "kind": "conversation",
                "id": "chat-1",
                "chat_id": "chat-1",
                "title": "Current chat",
                "text": "### Assistant\n# Previous report",
            }
        ],
    }
    second = await client.post("/v1/research-jobs", headers=second_headers, json=second_payload)
    assert second.status_code == 202
    assert second.json()["parent_job_id"] == first_id
    assert second.json()["iteration"] == 2
    assert second.json()["context_document_count"] == 1


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


async def test_terminal_runner_spec_is_rejected_for_legacy_retained_token(
    api_client: tuple[httpx.AsyncClient, Any],
    service_headers: dict[str, str],
    job_payload: dict[str, object],
) -> None:
    client, app = api_client
    created = await client.post("/v1/research-jobs", headers=service_headers, json=job_payload)
    job_id = created.json()["id"]
    async with app.state.database.session() as session, session.begin():
        claim = await app.state.repository.claim_next(
            session, max_concurrent_jobs=5, lease_seconds=120
        )
        assert claim is not None
        job = await app.state.repository.get_for_runner_locked(
            session, job_id=claim.id, runner_token=claim.runner_token
        )
        job.state = "succeeded"

    response = await client.get(
        f"/internal/jobs/{job_id}",
        headers={"Authorization": f"Bearer {claim.runner_token}"},
    )
    assert response.status_code == 409
    assert response.json()["detail"] == "job is terminal"

    cancel = await client.post(f"/v1/research-jobs/{job_id}:cancel", headers=service_headers)
    assert cancel.status_code == 200
    async with app.state.database.session() as session:
        stored = await app.state.repository.get_for_user(session, job_id=claim.id, user_id="user-1")
        assert stored.runner_token_hash is None


async def test_query_limit_and_ownership_are_enforced(
    api_client: tuple[httpx.AsyncClient, Any],
    service_headers: dict[str, str],
    job_payload: dict[str, object],
) -> None:
    client, _ = api_client
    too_large = dict(job_payload)
    too_large["budget"] = {
        "max_queries": 101,
        "max_wall_time_seconds": 300,
    }
    rejected = await client.post("/v1/research-jobs", headers=service_headers, json=too_large)
    assert rejected.status_code == 422

    created = await client.post("/v1/research-jobs", headers=service_headers, json=job_payload)
    job_id = created.json()["id"]
    other_user = {**service_headers, "X-OpenWebUI-User-Id": "user-2"}
    assert (await client.get(f"/v1/research-jobs/{job_id}", headers=other_user)).status_code == 404


@pytest.mark.parametrize(
    ("research", "max_queries"),
    [
        ({"strategy": "focused", "breadth": 1, "depth": 1, "queries_per_branch": 2}, 5),
        ({"strategy": "balanced", "breadth": 2, "depth": 2, "queries_per_branch": 2}, 25),
        ({"strategy": "broad", "breadth": 4, "depth": 2, "queries_per_branch": 2}, 49),
        ({"strategy": "deep", "breadth": 2, "depth": 3, "queries_per_branch": 3}, 71),
        ({"strategy": "custom", "breadth": 3, "depth": 3, "queries_per_branch": 1}, 64),
    ],
)
async def test_each_research_shape_is_frozen_on_submission(
    api_client: tuple[httpx.AsyncClient, Any],
    service_headers: dict[str, str],
    job_payload: dict[str, object],
    research: dict[str, object],
    max_queries: int,
) -> None:
    client, _ = api_client
    payload = copy.deepcopy(job_payload)
    payload["research"] = research
    payload["budget"]["max_queries"] = max_queries  # type: ignore[index]
    response = await client.post("/v1/research-jobs", headers=service_headers, json=payload)
    assert response.status_code == 202
    assert response.json()["research"] == research
    assert response.json()["budget"]["max_queries"] == max_queries


async def test_runner_failure_events_and_private_search_telemetry(
    api_client: tuple[httpx.AsyncClient, Any],
    service_headers: dict[str, str],
    job_payload: dict[str, object],
) -> None:
    client, app = api_client
    payload = copy.deepcopy(job_payload)
    payload["context_documents"] = [
        {
            "kind": "conversation",
            "id": "chat-1",
            "chat_id": "chat-1",
            "title": "Research context",
            "text": "private conversation evidence",
        }
    ]
    created = await client.post("/v1/research-jobs", headers=service_headers, json=payload)
    job_id = created.json()["id"]
    async with app.state.database.session() as session, session.begin():
        claim = await app.state.repository.claim_next(
            session, max_concurrent_jobs=5, lease_seconds=120
        )
    assert claim is not None
    runner_headers = {"Authorization": f"Bearer {claim.runner_token}"}
    assert (await client.get(f"/internal/jobs/{job_id}", headers=runner_headers)).status_code == 200
    assert (
        await client.post(f"/internal/jobs/{job_id}/started", headers=runner_headers)
    ).status_code == 200
    for _ in range(6):
        search = await client.post(
            f"/internal/jobs/{job_id}/search",
            headers=runner_headers,
            json={"query": "private query"},
        )
        assert search.json() == [
            {
                "text": "private conversation evidence",
                "metadata": {
                    "source": "openwebui-chat:chat-1",
                    "file_id": "chat-1",
                    "name": "Research context",
                    "kind": "conversation",
                },
            }
        ]
    async with app.state.database.session() as session:
        job = await app.state.repository.get_for_runner(
            session, job_id=claim.id, runner_token=claim.runner_token
        )
        assert job.usage["private_searches"] == 6
        assert job.usage["searches"] == 0
    failed = await client.post(
        f"/internal/jobs/{job_id}/failed",
        headers=runner_headers,
        json={"error": "provider failed"},
    )
    assert failed.json() == {"state": "failed"}
    assert (await client.get(f"/internal/jobs/{job_id}", headers=runner_headers)).status_code == 404
    async with app.state.database.session() as session:
        stored = await app.state.repository.get_for_user(
            session, job_id=UUID(job_id), user_id="user-1"
        )
        assert stored.runner_token_hash is None
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
        claim = await app.state.repository.claim_next(
            session, max_concurrent_jobs=5, lease_seconds=120
        )
    assert claim is not None
    runner_headers = {"Authorization": f"Bearer {claim.runner_token}"}
    cancelled = await client.post(f"/v1/research-jobs/{job_id}:cancel", headers=service_headers)
    assert cancelled.json()["state"] == "cancel_requested"
    started = await client.post(f"/internal/jobs/{job_id}/started", headers=runner_headers)
    assert started.json() == {"state": "cancel_requested"}
    state = await client.get(f"/internal/jobs/{job_id}/state", headers=runner_headers)
    assert state.json() == {"state": "cancel_requested"}
    acknowledged = await client.post(f"/internal/jobs/{job_id}/cancelled", headers=runner_headers)
    assert acknowledged.json() == {"state": "cancelled"}
    assert (await client.get(f"/internal/jobs/{job_id}", headers=runner_headers)).status_code == 404
    async with app.state.database.session() as session:
        stored = await app.state.repository.get_for_user(
            session, job_id=UUID(job_id), user_id="user-1"
        )
        assert stored.runner_token_hash is None


async def test_model_and_embedding_proxies_account_usage(
    api_client: tuple[httpx.AsyncClient, Any],
    service_headers: dict[str, str],
    job_payload: dict[str, object],
) -> None:
    client, app = api_client
    created = await client.post("/v1/research-jobs", headers=service_headers, json=job_payload)
    job_id = created.json()["id"]
    async with app.state.database.session() as session, session.begin():
        claim = await app.state.repository.claim_next(
            session, max_concurrent_jobs=5, lease_seconds=120
        )
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
    app.state.settings.reasoning_effort = "low"
    app.state.openwebui.proxy_embeddings = AsyncMock(
        return_value=httpx.Response(200, json={"data": [{"embedding": [0.1]}]})
    )
    completion = await client.post(
        f"/internal/jobs/{job_id}/openai/v1/chat/completions",
        headers=runner_headers,
        json={"model": "test-model", "messages": [], "max_tokens": 5_000},
    )
    assert completion.status_code == 200
    proxied = app.state.openwebui.proxy_chat_completions.await_args.kwargs["payload"]
    assert proxied["reasoning_effort"] == "low"
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
        assert job.usage["estimated_token_calls"] == 0


async def test_model_proxy_preserves_streaming_and_accounts_final_usage(
    api_client: tuple[httpx.AsyncClient, Any],
    service_headers: dict[str, str],
    job_payload: dict[str, object],
) -> None:
    client, app = api_client
    created = await client.post("/v1/research-jobs", headers=service_headers, json=job_payload)
    job_id = created.json()["id"]
    async with app.state.database.session() as session, session.begin():
        claim = await app.state.repository.claim_next(
            session, max_concurrent_jobs=5, lease_seconds=120
        )
    assert claim is not None
    runner_headers = {"Authorization": f"Bearer {claim.runner_token}"}
    stream_body = b"".join(
        [
            b'data: {"choices":[{"delta":{"content":"streamed "}}]}\n\n',
            b'data: {"choices":[{"delta":{"content":"answer"}}]}\n\n',
            b'data: {"choices":[],"usage":{"prompt_tokens":41,"completion_tokens":2}}\n\n',
            b"data: [DONE]\n\n",
        ]
    )
    app.state.openwebui.proxy_chat_completions_stream = AsyncMock(
        return_value=httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=stream_body,
        )
    )

    response = await client.post(
        f"/internal/jobs/{job_id}/openai/v1/chat/completions",
        headers=runner_headers,
        json={
            "model": "test-model",
            "messages": [{"role": "user", "content": "question"}],
            "stream": True,
        },
    )

    assert response.status_code == 200
    assert response.content == stream_body
    assert response.headers["content-type"].startswith("text/event-stream")
    proxied = app.state.openwebui.proxy_chat_completions_stream.await_args.kwargs["payload"]
    assert proxied["stream"] is True
    assert proxied["stream_options"] == {"include_usage": True}
    async with app.state.database.session() as session:
        job = await app.state.repository.get_for_runner(
            session, job_id=claim.id, runner_token=claim.runner_token
        )
        assert job.usage["input_tokens"] == 41
        assert job.usage["output_tokens"] == 2
        assert job.usage["estimated_token_calls"] == 0


async def test_embedding_proxy_batches_large_requests_and_preserves_order(
    api_client: tuple[httpx.AsyncClient, Any],
    service_headers: dict[str, str],
    job_payload: dict[str, object],
) -> None:
    client, app = api_client
    created = await client.post("/v1/research-jobs", headers=service_headers, json=job_payload)
    job_id = created.json()["id"]
    async with app.state.database.session() as session, session.begin():
        claim = await app.state.repository.claim_next(
            session, max_concurrent_jobs=5, lease_seconds=120
        )
    assert claim is not None
    app.state.settings.embedding_batch_size = 2
    calls: list[list[str]] = []

    async def proxy_embeddings(*, payload: dict[str, Any], model: str) -> httpx.Response:
        batch = payload["input"]
        calls.append(batch)
        return httpx.Response(
            200,
            json={
                "object": "list",
                "model": model,
                "data": [
                    {"object": "embedding", "index": index, "embedding": [float(len(text))]}
                    for index, text in enumerate(batch)
                ],
                "usage": {"prompt_tokens": len(batch), "total_tokens": len(batch)},
            },
        )

    app.state.openwebui.proxy_embeddings = proxy_embeddings
    response = await client.post(
        f"/internal/jobs/{job_id}/openai/v1/embeddings",
        headers={"Authorization": f"Bearer {claim.runner_token}"},
        json={"input": ["a", "bb", "ccc", "dddd", "eeeee"]},
    )
    assert response.status_code == 200
    assert calls == [["a", "bb"], ["ccc", "dddd"], ["eeeee"]]
    payload = response.json()
    assert [item["index"] for item in payload["data"]] == [0, 1, 2, 3, 4]
    assert [item["embedding"] for item in payload["data"]] == [
        [1.0],
        [2.0],
        [3.0],
        [4.0],
        [5.0],
    ]
    assert payload["usage"] == {"prompt_tokens": 5, "total_tokens": 5}


async def test_model_proxy_estimates_usage_when_provider_omits_it(
    api_client: tuple[httpx.AsyncClient, Any],
    service_headers: dict[str, str],
    job_payload: dict[str, object],
) -> None:
    client, app = api_client
    created = await client.post("/v1/research-jobs", headers=service_headers, json=job_payload)
    job_id = created.json()["id"]
    async with app.state.database.session() as session, session.begin():
        claim = await app.state.repository.claim_next(
            session, max_concurrent_jobs=5, lease_seconds=120
        )
    assert claim is not None
    runner_headers = {"Authorization": f"Bearer {claim.runner_token}"}
    app.state.openwebui.proxy_chat_completions = AsyncMock(
        return_value=httpx.Response(
            200,
            json={"choices": [{"message": {"content": "an estimated answer"}}]},
        )
    )
    response = await client.post(
        f"/internal/jobs/{job_id}/openai/v1/chat/completions",
        headers=runner_headers,
        json={"model": "test-model", "messages": [{"role": "user", "content": "question"}]},
    )
    assert response.status_code == 200
    async with app.state.database.session() as session:
        job = await app.state.repository.get_for_runner(
            session, job_id=claim.id, runner_token=claim.runner_token
        )
        assert job.usage["input_tokens"] > 0
        assert job.usage["output_tokens"] > 0
        assert job.usage["estimated_token_calls"] == 1


async def test_public_search_is_accounted_and_uses_searx_gateway(
    api_client: tuple[httpx.AsyncClient, Any],
    service_headers: dict[str, str],
    job_payload: dict[str, object],
) -> None:
    client, app = api_client
    created = await client.post("/v1/research-jobs", headers=service_headers, json=job_payload)
    job_id = created.json()["id"]
    async with app.state.database.session() as session, session.begin():
        claim = await app.state.repository.claim_next(
            session, max_concurrent_jobs=5, lease_seconds=120
        )
    assert claim is not None
    runner_headers = {"Authorization": f"Bearer {claim.runner_token}"}
    await client.post(f"/internal/jobs/{job_id}/started", headers=runner_headers)
    app.state.public_search.search = AsyncMock(
        return_value=[{"url": "https://example.com", "title": "Example", "body": "text"}]
    )
    for index in range(5):
        response = await client.post(
            f"/internal/jobs/{job_id}/public-search",
            headers=runner_headers,
            json={
                "query": f"query {index}",
                "max_results": 3,
                "domains": ["example.com"],
            },
        )
        assert response.status_code == 200
    exhausted = await client.post(
        f"/internal/jobs/{job_id}/public-search",
        headers=runner_headers,
        json={"query": "one too many", "max_results": 3},
    )
    assert exhausted.status_code == 429
    assert exhausted.json()["detail"] == "public search query limit exhausted"
    assert app.state.public_search.search.await_count == 5
    app.state.public_search.search.assert_any_await(
        query="query 0", max_results=3, domains=["example.com"]
    )
    async with app.state.database.session() as session:
        job = await app.state.repository.get_for_user(session, job_id=claim.id, user_id="user-1")
        assert job.usage["searches"] == 5


async def test_oversized_prompt_fails_before_openwebui_call(
    api_client: tuple[httpx.AsyncClient, Any],
    service_headers: dict[str, str],
    job_payload: dict[str, object],
) -> None:
    client, app = api_client
    created = await client.post("/v1/research-jobs", headers=service_headers, json=job_payload)
    job_id = created.json()["id"]
    async with app.state.database.session() as session, session.begin():
        claim = await app.state.repository.claim_next(
            session, max_concurrent_jobs=5, lease_seconds=120
        )
    assert claim is not None
    runner_headers = {"Authorization": f"Bearer {claim.runner_token}"}
    await client.post(f"/internal/jobs/{job_id}/started", headers=runner_headers)
    async with app.state.database.session() as session, session.begin():
        job_row = await app.state.repository.get_for_runner_locked(
            session, job_id=UUID(job_id), runner_token=claim.runner_token
        )
        job_row.model_capabilities = [
            {"id": "test-model", "context_length": 4_096, "max_output_tokens": 2_000}
        ]
    app.state.openwebui.proxy_chat_completions = AsyncMock()
    response = await client.post(
        f"/internal/jobs/{job_id}/openai/v1/chat/completions",
        headers=runner_headers,
        json={"model": "test-model", "messages": [{"role": "user", "content": "x" * 40_000}]},
    )
    assert response.status_code == 422
    assert "approximately" in response.json()["detail"]
    app.state.openwebui.proxy_chat_completions.assert_not_awaited()
    job = await client.get(f"/v1/research-jobs/{job_id}", headers=service_headers)
    assert job.json()["state"] == "failed"
    assert "model context exhausted" in job.json()["error"]


async def test_output_limit_is_capped_to_model_context_window(
    api_client: tuple[httpx.AsyncClient, Any],
    service_headers: dict[str, str],
    job_payload: dict[str, object],
) -> None:
    client, app = api_client
    created = await client.post("/v1/research-jobs", headers=service_headers, json=job_payload)
    job_id = created.json()["id"]
    async with app.state.database.session() as session, session.begin():
        claim = await app.state.repository.claim_next(
            session, max_concurrent_jobs=5, lease_seconds=120
        )
    assert claim is not None
    runner_headers = {"Authorization": f"Bearer {claim.runner_token}"}
    await client.post(f"/internal/jobs/{job_id}/started", headers=runner_headers)
    async with app.state.database.session() as session, session.begin():
        job_row = await app.state.repository.get_for_runner_locked(
            session, job_id=UUID(job_id), runner_token=claim.runner_token
        )
        job_row.model_capabilities = [
            {"id": "test-model", "context_length": 4_096, "max_output_tokens": 2_000}
        ]
    app.state.settings.model_context_safety_tokens = 256
    app.state.openwebui.proxy_chat_completions = AsyncMock(
        return_value=httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "answer"}}],
                "usage": {"prompt_tokens": 3_000, "completion_tokens": 100},
            },
        )
    )
    response = await client.post(
        f"/internal/jobs/{job_id}/openai/v1/chat/completions",
        headers=runner_headers,
        json={
            "model": "test-model",
            "messages": [{"role": "user", "content": "x" * 9_000}],
            "max_tokens": 5_000,
        },
    )
    assert response.status_code == 200
    payload = app.state.openwebui.proxy_chat_completions.await_args.kwargs["payload"]
    assert 1 <= payload["max_tokens"] < 1_000


async def test_health_endpoints(
    api_client: tuple[httpx.AsyncClient, Any],
) -> None:
    client, _ = api_client
    assert (await client.get("/health/live")).json() == {"status": "ok"}
    assert (await client.get("/health/ready")).json() == {"status": "ok"}
