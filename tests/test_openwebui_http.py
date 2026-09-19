from __future__ import annotations

import httpx
import pytest

from open_webui_gpt_researcher.domain import SourceRef
from open_webui_gpt_researcher.openwebui import OpenWebUIClient, OpenWebUIError


def make_client(handler: httpx.MockTransport) -> OpenWebUIClient:
    client = OpenWebUIClient(base_url="http://openwebui", api_key="key")
    client._client = httpx.AsyncClient(transport=handler, base_url="http://openwebui")
    return client


async def test_openwebui_gateway_operations() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/query/collection"):
            return httpx.Response(200, json={"documents": [["collection text"]]})
        if request.url.path.endswith("/query/doc"):
            return httpx.Response(200, json={"documents": [["file text"]]})
        return httpx.Response(200, json={"ok": True})

    client = make_client(httpx.MockTransport(handler))
    passages = await client.retrieve(
        query="query",
        sources=[
            SourceRef(kind="collection", id="kb"),
            SourceRef(kind="file", id="file"),
        ],
    )
    assert [item.text for item in passages] == ["collection text", "file text"]
    await client.emit_message_event(
        chat_id="chat", message_id="message", event_type="status", data={}
    )
    assert requests
    await client.close()


async def test_gateway_rejects_model_and_write_access() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"user_id": "owner", "access_grants": []})
        return httpx.Response(500, text="provider failed")

    client = make_client(httpx.MockTransport(handler))
    with pytest.raises(OpenWebUIError, match="outside its frozen selection"):
        await client.proxy_chat_completions(
            payload={"model": "wrong"},
            allowed_models={"fast", "smart"},
            default_model="smart",
        )
    with pytest.raises(OpenWebUIError, match="returned 500"):
        await client.proxy_embeddings(payload={"input": "text"}, model="embed")
    await client.close()


async def test_gateway_streams_chat_completions_without_buffering() -> None:
    body = b'data: {"choices":[{"delta":{"content":"answer"}}]}\n\ndata: [DONE]\n\n'

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/chat/completions"
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=body,
        )

    client = make_client(httpx.MockTransport(handler))
    response = await client.proxy_chat_completions_stream(
        payload={"model": "smart", "stream": True},
        allowed_models={"smart"},
        default_model="smart",
    )
    assert b"".join([chunk async for chunk in response.aiter_bytes()]) == body
    await response.aclose()
    await client.close()


async def test_list_models_can_use_user_or_integration_authorization() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"data": [{"id": "model-1"}]})

    client = make_client(httpx.MockTransport(handler))
    assert await client.list_models(authorization="Bearer user-token") == [{"id": "model-1"}]
    assert await client.list_models() == [{"id": "model-1"}]
    assert requests[0].headers["authorization"] == "Bearer user-token"
    await client.close()
