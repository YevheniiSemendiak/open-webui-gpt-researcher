"""
title: Deep Research
author: Open WebUI GPT Researcher contributors
version: 0.1.0
required_open_webui_version: 0.11.0
requirements: httpx>=0.28,<1
"""

from __future__ import annotations

import hashlib
from typing import Any

import httpx
from pydantic import BaseModel, Field


class Pipe:
    class Valves(BaseModel):
        service_url: str = "http://open-webui-gpt-researcher:8090"
        service_token: str = ""
        public_search_enabled: bool = True
        require_plan_approval: bool = True
        default_input_tokens: int = Field(default=120_000, ge=1_000)
        default_output_tokens: int = Field(default=24_000, ge=1_000)
        default_searches: int = Field(default=30, ge=1)
        default_wall_time_seconds: int = Field(default=3_600, ge=60)
        model_profile: str = "default"

    class UserValves(BaseModel):
        max_input_tokens: int | None = Field(default=None, ge=1_000)
        max_output_tokens: int | None = Field(default=None, ge=1_000)
        max_searches: int | None = Field(default=None, ge=1)
        max_wall_time_seconds: int | None = Field(default=None, ge=60)

    def __init__(self) -> None:
        self.valves = self.Valves()

    async def pipe(
        self,
        body: dict[str, Any],
        __user__: dict[str, Any],
        __chat_id__: str | None = None,
        __message_id__: str | None = None,
        __files__: list[dict[str, Any]] | None = None,
        __event_emitter__: Any = None,
        __event_call__: Any = None,
    ) -> str:
        if not self.valves.service_token:
            return "Deep Research is not configured: the administrator must set a service token."
        user_id = str(__user__.get("id", ""))
        if not user_id or not __chat_id__ or not __message_id__:
            return "Deep Research requires an authenticated chat session."
        query = self._last_user_message(body)
        if not query:
            return "Please provide a research question."

        budget = self._budget(__user__.get("valves"))
        sources = self._sources(__files__ or [])
        if not self.valves.public_search_enabled and not sources:
            return (
                "Public search is disabled. Attach a file or select an Open WebUI "
                "Knowledge collection before starting deep research."
            )
        source_summary = f"{len(sources)} attached file/knowledge source(s)"
        source_scope = (
            f"public web plus {source_summary}"
            if self.valves.public_search_enabled
            else f"only {source_summary}; public web is disabled"
        )
        plan = (
            f"Question: {query}\n\n"
            f"Sources: {source_scope}\n\n"
            f"Budget: up to {budget['max_input_tokens']:,} input tokens, "
            f"{budget['max_output_tokens']:,} output tokens, {budget['max_searches']} searches, "
            f"and {budget['max_wall_time_seconds'] // 60} minutes."
        )
        if self.valves.require_plan_approval:
            if __event_call__ is None:
                return "Deep Research needs an active browser session to approve the research plan."
            approved = await __event_call__(
                {
                    "type": "confirmation",
                    "data": {"title": "Start deep research?", "message": plan},
                }
            )
            if not approved:
                return "Deep research was not started."

        if __event_emitter__ is not None:
            await __event_emitter__(
                {
                    "type": "status",
                    "data": {
                        "description": "Deep research queued",
                        "done": False,
                    },
                }
            )
        request = {
            "query": query,
            "chat_id": __chat_id__,
            "message_id": __message_id__,
            "sources": sources,
            "budget": budget,
            "model_profile": self.valves.model_profile,
            "report_type": "deep",
            "report_formats": ["markdown", "json"],
        }
        idempotency_key = hashlib.sha256(
            f"{user_id}:{__chat_id__}:{__message_id__}".encode()
        ).hexdigest()
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.post(
                    f"{self.valves.service_url.rstrip('/')}/v1/research-jobs",
                    headers={
                        "X-Research-Service-Token": self.valves.service_token,
                        "X-OpenWebUI-User-Id": user_id,
                        "Idempotency-Key": idempotency_key,
                    },
                    json=request,
                )
                response.raise_for_status()
                job_id = response.json()["id"]
        except httpx.HTTPStatusError as error:
            detail = error.response.text[:1_000]
            return f"Deep Research rejected the job: {detail}"
        except httpx.HTTPError as error:
            return f"Deep Research service is unavailable: {error}"
        return (
            f"Deep research started (job `{job_id}`). You can leave this chat; "
            "the report and download links will appear here when it finishes."
        )

    def _budget(self, user_valves: Any) -> dict[str, int]:
        values = user_valves or self.UserValves()

        def pick(name: str, default: int) -> int:
            value = values.get(name) if isinstance(values, dict) else getattr(values, name, None)
            return int(value) if value is not None else default

        return {
            "max_input_tokens": pick("max_input_tokens", self.valves.default_input_tokens),
            "max_output_tokens": pick("max_output_tokens", self.valves.default_output_tokens),
            "max_searches": pick("max_searches", self.valves.default_searches),
            "max_wall_time_seconds": pick(
                "max_wall_time_seconds", self.valves.default_wall_time_seconds
            ),
        }

    @staticmethod
    def _last_user_message(body: dict[str, Any]) -> str:
        for message in reversed(body.get("messages", [])):
            if message.get("role") != "user":
                continue
            content = message.get("content", "")
            if isinstance(content, str):
                return content.strip()
            if isinstance(content, list):
                return "\n".join(
                    item.get("text", "")
                    for item in content
                    if isinstance(item, dict) and item.get("type") == "text"
                ).strip()
        return ""

    @staticmethod
    def _sources(files: list[dict[str, Any]]) -> list[dict[str, str]]:
        result: list[dict[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for item in files:
            source_type = str(item.get("type", "file"))
            kind = "collection" if source_type in {"collection", "knowledge"} else "file"
            nested = item.get("file") if isinstance(item.get("file"), dict) else {}
            source_id = str(item.get("id") or nested.get("id") or "")
            if not source_id or (kind, source_id) in seen:
                continue
            seen.add((kind, source_id))
            result.append(
                {
                    "kind": kind,
                    "id": source_id,
                    "name": str(item.get("name") or nested.get("filename") or source_id),
                }
            )
        return result
