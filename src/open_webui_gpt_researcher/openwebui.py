from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import httpx

from .domain import SourceRef


class OpenWebUIError(RuntimeError):
    pass


@dataclass(frozen=True)
class RetrievedPassage:
    text: str
    metadata: dict[str, object]


class OpenWebUIClient:
    """Narrow, server-side adapter over supported Open WebUI APIs."""

    def __init__(self, *, base_url: str, api_key: str, timeout: float = 60.0) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def retrieve(
        self, *, query: str, sources: list[SourceRef], k: int = 10
    ) -> list[RetrievedPassage]:
        passages: list[RetrievedPassage] = []
        collections = [source.id for source in sources if source.kind == "collection"]
        if collections:
            response = await self._request(
                "POST",
                "/api/v1/retrieval/query/collection",
                json={"collection_names": collections, "query": query, "k": k},
            )
            passages.extend(self._parse_retrieval_response(self._checked_json(response)))

        for source in (item for item in sources if item.kind == "file"):
            response = await self._request(
                "POST",
                "/api/v1/retrieval/query/doc",
                json={"collection_name": f"file-{source.id}", "query": query, "k": k},
            )
            passages.extend(self._parse_retrieval_response(self._checked_json(response)))
        return passages

    async def emit_message_event(
        self,
        *,
        chat_id: str,
        message_id: str,
        event_type: str,
        data: dict[str, object],
    ) -> None:
        response = await self._request(
            "POST",
            f"/api/v1/chats/{chat_id}/messages/{message_id}/event",
            json={"type": event_type, "data": data},
        )
        self._raise_for_status(response)

    async def proxy_chat_completions(
        self,
        *,
        payload: dict[str, Any],
        allowed_models: set[str],
        default_model: str,
    ) -> httpx.Response:
        requested_model = payload.get("model")
        if requested_model not in allowed_models | {None}:
            raise OpenWebUIError("runner attempted to use a model outside its frozen selection")
        proxied = dict(payload)
        proxied["model"] = requested_model or default_model
        response = await self._request("POST", "/api/chat/completions", json=proxied)
        self._raise_for_status(response)
        return response

    async def proxy_chat_completions_stream(
        self,
        *,
        payload: dict[str, Any],
        allowed_models: set[str],
        default_model: str,
    ) -> httpx.Response:
        """Open an unbuffered OpenWebUI chat-completion response."""
        requested_model = payload.get("model")
        if requested_model not in allowed_models | {None}:
            raise OpenWebUIError("runner attempted to use a model outside its frozen selection")
        proxied = dict(payload)
        proxied["model"] = requested_model or default_model
        request = self._client.build_request(
            "POST",
            "/api/chat/completions",
            json=proxied,
        )
        try:
            response = await self._client.send(request, stream=True)
        except httpx.HTTPError as error:
            raise OpenWebUIError(f"Open WebUI request failed: {error}") from error
        if response.is_error:
            try:
                body = (await response.aread()).decode(errors="replace")[:2_000]
            finally:
                await response.aclose()
            raise OpenWebUIError(f"Open WebUI returned {response.status_code}: {body}")
        return response

    async def proxy_embeddings(self, *, payload: dict[str, Any], model: str) -> httpx.Response:
        proxied = dict(payload)
        proxied["model"] = model
        response = await self._request("POST", "/api/v1/embeddings", json=proxied)
        self._raise_for_status(response)
        return response

    async def list_models(self, *, authorization: str | None = None) -> list[dict[str, Any]]:
        headers = {"Authorization": authorization} if authorization else None
        response = await self._request("GET", "/api/models", headers=headers)
        payload = self._checked_json(response)
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            raise OpenWebUIError("Open WebUI returned an invalid model catalog")
        return [item for item in payload["data"] if isinstance(item, dict) and item.get("id")]

    @staticmethod
    def _parse_retrieval_response(payload: object) -> list[RetrievedPassage]:
        if isinstance(payload, dict):
            payload = [payload]
        if not isinstance(payload, list):
            return []
        passages: list[RetrievedPassage] = []
        for group in payload:
            if not isinstance(group, dict):
                continue
            documents = group.get("documents", [])
            metadatas = group.get("metadatas", [])
            if documents and isinstance(documents[0], list):
                documents = documents[0]
            if metadatas and isinstance(metadatas[0], list):
                metadatas = metadatas[0]
            for index, text in enumerate(documents if isinstance(documents, list) else []):
                if not isinstance(text, str):
                    continue
                metadata: dict[str, object] = {}
                if isinstance(metadatas, list) and index < len(metadatas):
                    candidate = metadatas[index]
                    if isinstance(candidate, dict):
                        metadata = candidate
                passages.append(RetrievedPassage(text=text, metadata=metadata))
        return passages

    async def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        try:
            return await self._client.request(method, url, **kwargs)
        except httpx.HTTPError as error:
            raise OpenWebUIError(f"Open WebUI request failed: {error}") from error

    @staticmethod
    def _raise_for_status(response: httpx.Response) -> None:
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as error:
            body = response.text[:2_000]
            raise OpenWebUIError(f"Open WebUI returned {response.status_code}: {body}") from error

    @classmethod
    def _checked_json(cls, response: httpx.Response) -> dict[str, Any] | list[Any]:
        cls._raise_for_status(response)
        try:
            payload: dict[str, Any] | list[Any] = response.json()
        except json.JSONDecodeError as error:
            raise OpenWebUIError("Open WebUI returned invalid JSON") from error
        return payload
