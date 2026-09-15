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
        if request.url.path == "/api/v1/files/":
            return httpx.Response(200, json={"id": "file-id"})
        if request.url.path == "/api/v1/knowledge/create":
            return httpx.Response(200, json={"id": "knowledge-id"})
        if request.url.path == "/api/v1/knowledge/knowledge-id":
            return httpx.Response(200, json={"user_id": "user-id", "access_grants": []})
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
    assert await client.upload_markdown(name="r.md", content=b"report") == "file-id"
    assert await client.create_knowledge(name="Research", owner_user_id="user-id") == "knowledge-id"
    await client.assert_knowledge_write_access(knowledge_id="knowledge-id", user_id="user-id")
    await client.add_file_to_knowledge(knowledge_id="knowledge-id", file_id="file-id")
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
    with pytest.raises(OpenWebUIError, match="outside its profile"):
        await client.proxy_chat_completions(payload={"model": "wrong"}, allowed_model="allowed")
    with pytest.raises(OpenWebUIError, match="write access"):
        await client.assert_knowledge_write_access(knowledge_id="knowledge-id", user_id="other")
    with pytest.raises(OpenWebUIError, match="returned 500"):
        await client.proxy_embeddings(payload={"input": "text"}, model="embed")
    await client.close()
