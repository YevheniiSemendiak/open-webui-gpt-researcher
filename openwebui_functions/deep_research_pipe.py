"""
title: Deep Research
author: Open WebUI GPT Researcher contributors
version: 0.0.0-dev
required_open_webui_version: 0.11.0
requirements: httpx>=0.28
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import AsyncIterator
from typing import Any, Literal
from urllib.parse import quote

import httpx
from pydantic import BaseModel, Field

RESEARCH_PRESETS: dict[str, tuple[int, int, int]] = {
    "focused": (1, 1, 2),
    "balanced": (2, 2, 2),
    "broad": (4, 2, 2),
    "deep": (2, 3, 3),
}


class Pipe:
    class Valves(BaseModel):
        service_url: str = "http://open-webui-gpt-researcher:8090"
        service_token: str = ""
        openwebui_url: str = "http://open-webui:8080"
        public_search_enabled: bool = True
        require_plan_approval: bool = True
        default_research_strategy: Literal["focused", "balanced", "broad", "deep"] = "balanced"
        default_max_queries: int = Field(default=100, ge=1)
        max_queries_cap: int = Field(
            default=100,
            ge=1,
            description="Infrastructure-enforced logical research-query limit per job.",
        )
        default_wall_time_seconds: int = Field(default=3_600, ge=60)
        default_models: dict[str, str] = Field(
            default_factory=lambda: {
                "fast": "gpt-4.1-mini",
                "smart": "gpt-4.1",
                "strategic": "gpt-4.1",
            }
        )
        context_char_limit: int = Field(default=120_000, ge=4_000, le=500_000)
        linked_chat_limit: int = Field(default=10, ge=0, le=50)
        job_poll_interval_seconds: float = Field(default=5.0, ge=1.0, le=30.0)
        completion_grace_seconds: int = Field(default=300, ge=30, le=3_600)

    class UserValves(BaseModel):
        research_strategy: Literal["focused", "balanced", "broad", "deep", "custom"] = Field(
            default="balanced",
            description=(
                "**Inherited default: `balanced` in the bundled configuration.** "
                "Choose Custom to use breadth, depth, and queries per branch."
            ),
        )
        breadth: int = Field(
            default=2,
            ge=1,
            le=8,
            description="**Custom default: `2`.** Initial parallel research directions.",
        )
        depth: int = Field(
            default=2,
            ge=1,
            le=4,
            description="**Custom default: `2`.** Recursive follow-up levels.",
        )
        queries_per_branch: int = Field(
            default=2,
            ge=1,
            le=5,
            description=(
                "**Custom default: `2`.** Generated search queries inside every research worker."
            ),
        )
        max_queries: int | None = Field(
            default=None,
            ge=1,
            description=(
                "**Inherited default: `100` in the bundled configuration.** "
                "Logical research-query limit, bounded by the administrator cap."
            ),
        )
        max_wall_time_seconds: int | None = Field(
            default=None,
            ge=60,
            description="**Inherited default: `3600` seconds in the bundled configuration.**",
        )

    def __init__(self) -> None:
        self.valves = self.Valves()

    async def pipe(
        self,
        body: dict[str, Any],
        __user__: dict[str, Any],
        __chat_id__: str | None = None,
        __message_id__: str | None = None,
        __files__: list[dict[str, Any]] | None = None,
        __request__: Any = None,
        __event_emitter__: Any = None,
        __event_call__: Any = None,
    ) -> str | AsyncIterator[str]:
        if not self.valves.service_token:
            return "Deep Research is not configured: the administrator must set a service token."
        user_id = str(__user__.get("id", ""))
        if not user_id or not __chat_id__ or not __message_id__:
            return "Deep Research requires an authenticated chat session."
        query = self._last_user_message(body)
        if not query:
            return "Please provide a research question."

        try:
            research = self._research_shape(__user__.get("valves"))
            budget = self._budget(__user__.get("valves"), research=research)
        except ValueError as error:
            return f"Deep Research configuration is invalid: {error}"
        authorization = self._authorization(__request__)
        if authorization is None:
            return "Deep Research requires the initiating user's Open WebUI authorization."
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                context_documents, sources, linked_chat_count = await self._collect_context(
                    client,
                    body=body,
                    chat_id=__chat_id__,
                    files=__files__ or [],
                    authorization=authorization,
                )
                available_models = await self._available_models(
                    client,
                    authorization=authorization,
                    user_id=user_id,
                )
                models = await self._select_models(available_models, event_call=__event_call__)
                if isinstance(models, str):
                    return models
                selected_models, model_capabilities = models
                response = None
                source_summary = f"{len(sources)} file/Knowledge source(s)"
                context_summary = (
                    f"{len(context_documents)} conversation context document(s), including "
                    f"{linked_chat_count} linked chat(s)"
                )
                if not self.valves.public_search_enabled and not sources and not context_documents:
                    return (
                        "Public search is disabled. Attach a file, select an Open WebUI "
                        "Knowledge collection, or continue an existing research chat."
                    )
                source_scope = (
                    f"public web plus {source_summary} and {context_summary}"
                    if self.valves.public_search_enabled
                    else f"only {source_summary} and {context_summary}; public web is disabled"
                )
                request_preview = self._request_preview(query)
                request_label = (
                    f"Request preview ({len(query):,} characters; "
                    "the full request will be used unchanged)"
                    if request_preview != query
                    else "Request"
                )
                plan = (
                    f"{request_label}:\n\n{request_preview}\n\n"
                    f"Sources: {source_scope}.\n\n"
                    f"Research strategy: {research['strategy']} — breadth "
                    f"{research['breadth']}, depth {research['depth']}, "
                    f"{research['queries_per_branch']} queries per branch "
                    f"(up to {self._estimated_max_queries(research)} planned queries).\n\n"
                    f"Limits: up to {budget['max_queries']} search queries "
                    f"and {budget['max_wall_time_seconds'] // 60} minutes.\n\n"
                    f"Models: fast `{selected_models['fast']}`, "
                    f"smart `{selected_models['smart']}`, and "
                    f"strategic `{selected_models['strategic']}`."
                )
                if self.valves.require_plan_approval:
                    if __event_call__ is None:
                        return (
                            "Deep Research needs an active browser session to approve the "
                            "research plan."
                        )
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
                                "description": "Starting deep research…",
                                "done": False,
                            },
                        }
                    )
                request = {
                    "query": query,
                    "chat_id": __chat_id__,
                    "message_id": __message_id__,
                    "sources": sources,
                    "context_documents": context_documents,
                    "research": research,
                    "budget": budget,
                    "models": selected_models,
                    "model_capabilities": model_capabilities,
                    "report_type": "deep",
                    "report_formats": ["markdown", "json"],
                }
                idempotency_key = hashlib.sha256(
                    f"{user_id}:{__chat_id__}:{__message_id__}".encode()
                ).hexdigest()
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
                payload = response.json()
                job_id = payload["id"]
                iteration = int(payload.get("iteration", 1))
                parent_job_id = payload.get("parent_job_id")
        except httpx.HTTPStatusError as error:
            detail = error.response.text[:1_000]
            return f"Deep Research rejected the job: {detail}"
        except httpx.HTTPError as error:
            return f"Deep Research service is unavailable: {error}"
        continuation = (
            f" Continuing research iteration {iteration} from job `{parent_job_id}`."
            if parent_job_id
            else ""
        )
        started = (
            f"Deep research started (job `{job_id}`).{continuation} You can leave this chat; "
            "the report and its files will appear here automatically when it finishes."
        )
        return self._stream_job_result(
            job_id=str(job_id),
            user_id=user_id,
            authorization=authorization,
            started=started,
            max_wait_seconds=budget["max_wall_time_seconds"] + self.valves.completion_grace_seconds,
            event_emitter=__event_emitter__,
        )

    async def _available_models(
        self,
        client: httpx.AsyncClient,
        *,
        authorization: str,
        user_id: str,
    ) -> list[dict[str, Any]]:
        response = await client.get(
            f"{self.valves.service_url.rstrip('/')}/v1/models",
            headers={
                "Authorization": authorization,
                "X-Research-Service-Token": self.valves.service_token,
                "X-OpenWebUI-User-Id": user_id,
            },
        )
        response.raise_for_status()
        payload = response.json()
        models = payload.get("data", []) if isinstance(payload, dict) else []
        return [
            model
            for model in models
            if isinstance(model, dict) and model.get("id") and model.get("id") != "deep_research"
        ]

    async def _select_models(
        self,
        available: list[dict[str, Any]],
        *,
        event_call: Any,
    ) -> tuple[dict[str, str], list[dict[str, int | str]]] | str:
        catalog: dict[str, dict[str, int | str]] = {}
        incomplete: set[str] = set()
        available_ids: set[str] = set()
        for model in available:
            model_id = str(model["id"])
            available_ids.add(model_id)
            openai_metadata = model.get("openai")
            nested: dict[str, Any] = openai_metadata if isinstance(openai_metadata, dict) else {}
            context_length = model.get("context_length") or nested.get("context_length")
            max_output_tokens = model.get("max_output_tokens") or nested.get("max_output_tokens")
            if not isinstance(context_length, int) or not isinstance(max_output_tokens, int):
                incomplete.add(model_id)
                continue
            if context_length < 4_096 or max_output_tokens < 1:
                incomplete.add(model_id)
                continue
            catalog[model_id] = {
                "id": model_id,
                "context_length": context_length,
                "max_output_tokens": max_output_tokens,
            }

        configured = self.valves.default_models
        if not available_ids:
            return "Deep Research found no accessible underlying Open WebUI models."

        def choices(role: str) -> list[dict[str, str]]:
            default_id = configured.get(role)
            ordered = sorted(available_ids)
            if default_id in available_ids:
                ordered.remove(default_id)
                ordered.insert(0, default_id)
            return [
                {
                    "label": model_id,
                    "description": (
                        (
                            f"{catalog[model_id]['context_length']:,} context; "
                            f"{catalog[model_id]['max_output_tokens']:,} maximum output tokens"
                        )
                        if model_id in catalog
                        else (
                            "Open WebUI does not publish this model's limits; "
                            "you will be asked to configure them for this run"
                        )
                    ),
                }
                for model_id in ordered
            ]

        if event_call is None:
            selected = {role: configured.get(role, "") for role in ("fast", "smart", "strategic")}
            if any(model_id not in available_ids for model_id in selected.values()):
                return "Deep Research needs an active browser session to select accessible models."
        else:
            answer = await event_call(
                {
                    "type": "request:user_input",
                    "data": {
                        "allow_other": False,
                        "questions": [
                            {
                                "id": role,
                                "header": f"{role.title()} model",
                                "question": (
                                    f"Choose the Open WebUI model for the {role} research role."
                                ),
                                "options": choices(role),
                                "allow_other": False,
                            }
                            for role in ("fast", "smart", "strategic")
                        ],
                    },
                }
            )
            if not isinstance(answer, dict) or answer.get("status") != "answered":
                return "Deep research was not started."
            answers = answer.get("answers") or {}
            selected = {
                role: str((answers.get(role) or {}).get("label") or "")
                for role in ("fast", "smart", "strategic")
            }
            if any(model_id not in available_ids for model_id in selected.values()):
                return "Deep Research rejected an invalid or inaccessible model selection."

        incomplete_selected = sorted(
            model_id for model_id in set(selected.values()) if model_id in incomplete
        )
        if incomplete_selected and event_call is None:
            return (
                "Deep Research needs an active browser session to configure limits for: "
                + ", ".join(incomplete_selected)
            )
        for model_id in incomplete_selected:
            capability = await self._configure_model_limits(model_id, event_call=event_call)
            if isinstance(capability, str):
                return capability
            catalog[model_id] = capability

        capabilities = [catalog[model_id] for model_id in dict.fromkeys(selected.values())]
        return selected, capabilities

    async def _configure_model_limits(
        self,
        model_id: str,
        *,
        event_call: Any,
    ) -> dict[str, int | str] | str:
        validation_error = ""
        for _attempt in range(3):
            answer = await event_call(
                {
                    "type": "request:user_input",
                    "data": {
                        "title": f"Configure limits for {model_id}",
                        "allow_other": True,
                        "timeout_ms": 240_000,
                        "questions": [
                            {
                                "id": "context_length",
                                "header": "Context window",
                                "question": (
                                    f"{validation_error}Open WebUI does not publish the context "
                                    f"window for `{model_id}`. Enter the verified token limit "
                                    "for this deployment."
                                ),
                                "options": [
                                    {
                                        "label": "32768",
                                        "description": "32K tokens; verify with your provider",
                                    },
                                    {
                                        "label": "131072",
                                        "description": "128K tokens; verify with your provider",
                                    },
                                    {
                                        "label": "262144",
                                        "description": "256K tokens; verify with your provider",
                                    },
                                ],
                                "allow_other": True,
                            },
                            {
                                "id": "max_output_tokens",
                                "header": "Maximum output",
                                "question": (
                                    "Enter the verified maximum output-token limit for this "
                                    "deployment. It cannot exceed the context window."
                                ),
                                "options": [
                                    {
                                        "label": "8192",
                                        "description": (
                                            "8K output tokens; verify with your provider"
                                        ),
                                    },
                                    {
                                        "label": "32768",
                                        "description": (
                                            "32K output tokens; verify with your provider"
                                        ),
                                    },
                                    {
                                        "label": "65536",
                                        "description": (
                                            "64K output tokens; verify with your provider"
                                        ),
                                    },
                                ],
                                "allow_other": True,
                            },
                        ],
                    },
                }
            )
            if not isinstance(answer, dict) or answer.get("status") != "answered":
                return "Deep research was not started."

            answers = answer.get("answers")
            if not isinstance(answers, dict):
                validation_error = "Both limits are required. "
                continue
            context_length = self._parse_token_limit(answers.get("context_length"))
            max_output_tokens = self._parse_token_limit(answers.get("max_output_tokens"))
            if context_length is None or context_length < 4_096:
                validation_error = "Context window must be a whole number of at least 4,096. "
                continue
            if max_output_tokens is None or max_output_tokens < 1:
                validation_error = "Maximum output must be a positive whole number. "
                continue
            if max_output_tokens > context_length:
                validation_error = "Maximum output cannot exceed the context window. "
                continue
            return {
                "id": model_id,
                "context_length": context_length,
                "max_output_tokens": max_output_tokens,
            }

        return (
            f"Deep Research could not validate token limits for `{model_id}` after three "
            "attempts. The job was not started."
        )

    @staticmethod
    def _parse_token_limit(answer: Any) -> int | None:
        if not isinstance(answer, dict):
            return None
        value = answer.get("text") if answer.get("type") == "other" else answer.get("label")
        if not isinstance(value, str):
            return None
        normalized = value.strip().replace(",", "").replace("_", "").replace(" ", "")
        if not normalized.isdecimal():
            return None
        return int(normalized)

    async def _stream_job_result(
        self,
        *,
        job_id: str,
        user_id: str,
        authorization: str,
        started: str,
        max_wait_seconds: int,
        event_emitter: Any,
    ) -> AsyncIterator[str]:
        """Keep Open WebUI's native completion alive until the durable job finishes.

        Returning the report through the original pipe stream lets Open WebUI persist its
        structured ``output`` field. Server-side events remain useful for progress, but its
        public API cannot replace another user's structured assistant output after the request.
        """
        headers = {
            "X-Research-Service-Token": self.valves.service_token,
            "X-OpenWebUI-User-Id": user_id,
        }
        yield f"{started}\n\n"
        deadline = asyncio.get_running_loop().time() + max_wait_seconds
        async with httpx.AsyncClient(timeout=30) as client:
            while True:
                try:
                    response = await client.get(
                        f"{self.valves.service_url.rstrip('/')}/v1/research-jobs/{job_id}",
                        headers=headers,
                    )
                    response.raise_for_status()
                    job = response.json()
                except httpx.HTTPError as error:
                    if asyncio.get_running_loop().time() >= deadline:
                        yield f"\n\nDeep Research could not retrieve the completed job: {error}"
                        return
                    yield ""
                    await asyncio.sleep(self.valves.job_poll_interval_seconds)
                    continue

                state = job.get("state")
                if state == "succeeded":
                    attached_files: list[dict[str, Any]] = []
                    attachment_error: str | None = None
                    if event_emitter is not None:
                        try:
                            attached_files = await self._attach_artifacts(
                                client,
                                job_id=job_id,
                                user_id=user_id,
                                authorization=authorization,
                            )
                        except (
                            httpx.HTTPError,
                            KeyError,
                            TypeError,
                            ValueError,
                            UnicodeDecodeError,
                        ) as error:
                            attachment_error = str(error)
                    report = await client.get(
                        f"{self.valves.service_url.rstrip('/')}/v1/research-jobs/"
                        f"{job_id}/artifacts/report.md/content",
                        headers=headers,
                    )
                    report.raise_for_status()
                    if attached_files and event_emitter is not None:
                        await event_emitter({"type": "files", "data": {"files": attached_files}})
                    if attachment_error and event_emitter is not None:
                        await event_emitter(
                            {
                                "type": "notification",
                                "data": {
                                    "type": "warning",
                                    "content": (
                                        "The report completed, but its files could not be "
                                        f"attached automatically: {attachment_error}"
                                    ),
                                },
                            }
                        )
                    if event_emitter is not None:
                        await event_emitter(
                            {
                                "type": "status",
                                "data": {
                                    "description": "Deep research complete",
                                    "done": True,
                                    "job_id": job_id,
                                },
                            }
                        )
                    yield report.text
                    return
                if state == "failed":
                    failure_reason = str(job.get("error") or "the research worker failed")
                    yield f"\n\nDeep Research failed: {failure_reason}"
                    return
                if state == "cancelled":
                    yield "\n\nDeep Research was cancelled."
                    return
                if asyncio.get_running_loop().time() >= deadline:
                    yield (
                        "\n\nDeep Research is still running beyond this chat "
                        "request's wait window. "
                        f"The durable job is `{job_id}`."
                    )
                    return
                # Emit an empty OpenAI stream chunk as an application-level keepalive.
                yield ""
                await asyncio.sleep(self.valves.job_poll_interval_seconds)

    async def _attach_artifacts(
        self,
        client: httpx.AsyncClient,
        *,
        job_id: str,
        user_id: str,
        authorization: str,
    ) -> list[dict[str, Any]]:
        service_headers = {
            "X-Research-Service-Token": self.valves.service_token,
            "X-OpenWebUI-User-Id": user_id,
        }
        response = await client.get(
            f"{self.valves.service_url.rstrip('/')}/v1/research-jobs/{job_id}/artifacts",
            headers=service_headers,
        )
        response.raise_for_status()
        manifest = response.json()
        if not isinstance(manifest, list):
            raise httpx.HTTPError("Research service returned an invalid artifact manifest")

        uploaded: list[dict[str, Any]] = []
        for artifact in manifest:
            if not isinstance(artifact, dict) or not artifact.get("name"):
                continue
            name = str(artifact["name"])
            content_response = await client.get(
                f"{self.valves.service_url.rstrip('/')}/v1/research-jobs/{job_id}/artifacts/"
                f"{quote(name, safe='')}/content",
                headers=service_headers,
            )
            content_response.raise_for_status()
            media_type = content_response.headers.get("content-type", "application/octet-stream")
            upload = await client.post(
                f"{self.valves.openwebui_url.rstrip('/')}/api/v1/files/",
                params={"process": "false"},
                headers={"Authorization": authorization},
                files={"file": (name, content_response.content, media_type)},
                data={"metadata": json.dumps({"deep_research_job_id": job_id})},
            )
            upload.raise_for_status()
            payload = upload.json()
            file_id = str(payload["id"])
            if media_type.startswith("text/") or media_type.startswith("application/json"):
                preview = await client.post(
                    f"{self.valves.openwebui_url.rstrip('/')}/api/v1/files/"
                    f"{quote(file_id, safe='')}/data/content/update",
                    headers={"Authorization": authorization},
                    json={"content": content_response.content.decode("utf-8")},
                )
                preview.raise_for_status()
            metadata = payload.get("meta") or {}
            uploaded.append(
                {
                    "type": "file",
                    "id": file_id,
                    "url": file_id,
                    "name": str(metadata.get("name") or payload.get("filename") or name),
                    "size": int(metadata.get("size") or len(content_response.content)),
                    "content_type": str(metadata.get("content_type") or media_type),
                }
            )
        return uploaded

    def _budget(self, user_valves: Any, *, research: dict[str, Any]) -> dict[str, int]:
        values = user_valves or self.UserValves()

        def pick(name: str, default: int) -> int:
            value = values.get(name) if isinstance(values, dict) else getattr(values, name, None)
            return int(value) if value is not None else default

        max_queries = pick("max_queries", self.valves.default_max_queries)
        if max_queries > self.valves.max_queries_cap:
            raise ValueError(
                f"max_queries {max_queries} exceeds the administrator cap "
                f"{self.valves.max_queries_cap}"
            )
        required = self._estimated_max_queries(research)
        if required > max_queries:
            raise ValueError(
                f"{research['strategy']} research may require up to {required} queries, "
                f"but max_queries is {max_queries}"
            )
        return {
            "max_queries": max_queries,
            "max_wall_time_seconds": pick(
                "max_wall_time_seconds", self.valves.default_wall_time_seconds
            ),
        }

    def _research_shape(self, user_valves: Any) -> dict[str, int | str]:
        values = user_valves or self.UserValves()

        def pick(name: str, default: Any) -> Any:
            if isinstance(values, dict):
                value = values.get(name)
            else:
                # A concrete schema default makes Open WebUI render Literal values as a
                # selector. An unset field must still inherit the administrator's default.
                fields_set: set[str] = getattr(values, "model_fields_set", set())
                if name not in fields_set:
                    return default
                value = getattr(values, name, None)
            return value if value is not None else default

        strategy = str(pick("research_strategy", self.valves.default_research_strategy))
        if strategy == "custom":
            breadth = int(pick("breadth", 2))
            depth = int(pick("depth", 2))
            queries_per_branch = int(pick("queries_per_branch", 2))
            if not 1 <= breadth <= 8:
                raise ValueError("breadth must be between 1 and 8")
            if not 1 <= depth <= 4:
                raise ValueError("depth must be between 1 and 4")
            if not 1 <= queries_per_branch <= 5:
                raise ValueError("queries_per_branch must be between 1 and 5")
        else:
            try:
                breadth, depth, queries_per_branch = RESEARCH_PRESETS[strategy]
            except KeyError as error:
                raise ValueError(f"unknown research_strategy: {strategy}") from error
        return {
            "strategy": strategy,
            "breadth": breadth,
            "depth": depth,
            "queries_per_branch": queries_per_branch,
        }

    @staticmethod
    def _estimated_max_queries(research: dict[str, Any]) -> int:
        current_breadth = int(research["breadth"])
        workers_at_level = current_breadth
        total_workers = 0
        for level in range(int(research["depth"])):
            if level:
                current_breadth = max(2, current_breadth // 2)
                workers_at_level *= current_breadth
            total_workers += workers_at_level
        return 1 + total_workers * (int(research["queries_per_branch"]) + 2)

    @staticmethod
    def _request_preview(query: str, edge_chars: int = 500) -> str:
        if len(query) <= edge_chars * 2:
            return query
        return f"{query[:edge_chars]}\n...\n{query[-edge_chars:]}"

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
            if source_type not in {"file", "doc", "text", "collection", "knowledge"}:
                continue
            kind = "collection" if source_type in {"collection", "knowledge"} else "file"
            file_metadata = item.get("file")
            nested: dict[str, Any] = file_metadata if isinstance(file_metadata, dict) else {}
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

    async def _collect_context(
        self,
        client: httpx.AsyncClient,
        *,
        body: dict[str, Any],
        chat_id: str,
        files: list[dict[str, Any]],
        authorization: str | None,
    ) -> tuple[list[dict[str, str]], list[dict[str, str]], int]:
        documents: list[dict[str, str]] = []
        all_items = list(files)
        prior_messages = self._prior_messages(body.get("messages", []))
        if transcript := self._format_messages(prior_messages):
            documents.append(
                {
                    "kind": "conversation",
                    "id": chat_id,
                    "chat_id": chat_id,
                    "title": "Current Open WebUI chat",
                    "text": transcript,
                }
            )

        linked_ids = {
            str(item.get("id"))
            for item in files
            if isinstance(item, dict) and item.get("type") == "chat" and item.get("id")
        }
        if authorization is not None:
            current = await self._get_chat(client, chat_id=chat_id, authorization=authorization)
            current_chat = current.get("chat") or {}
            current_items = self._chat_items(current_chat)
            all_items.extend(current_items)
            linked_ids.update(
                str(item["id"])
                for item in current_items
                if item.get("type") == "chat" and item.get("id")
            )

            for linked_id in sorted(linked_ids)[: self.valves.linked_chat_limit]:
                linked = await self._get_chat(
                    client, chat_id=linked_id, authorization=authorization
                )
                linked_chat = linked.get("chat") or {}
                all_items.extend(self._chat_items(linked_chat))
                linked_messages = self._active_messages(linked_chat)
                if transcript := self._format_messages(linked_messages):
                    documents.append(
                        {
                            "kind": "linked_chat",
                            "id": linked_id,
                            "chat_id": linked_id,
                            "title": str(linked.get("title") or "Linked Open WebUI chat"),
                            "text": transcript,
                        }
                    )

        compacted = self._compact_documents(documents)
        return (
            compacted,
            self._sources(all_items),
            min(len(linked_ids), self.valves.linked_chat_limit),
        )

    async def _get_chat(
        self,
        client: httpx.AsyncClient,
        *,
        chat_id: str,
        authorization: str,
    ) -> dict[str, Any]:
        response = await client.get(
            f"{self.valves.openwebui_url.rstrip('/')}/api/v1/chats/{quote(chat_id, safe='')}",
            headers={"Authorization": authorization},
        )
        response.raise_for_status()
        payload = response.json()
        return payload if isinstance(payload, dict) else {}

    @classmethod
    def _chat_items(cls, chat: dict[str, Any]) -> list[dict[str, Any]]:
        items = [item for item in chat.get("files", []) if isinstance(item, dict)]
        messages = ((chat.get("history") or {}).get("messages") or {}).values()
        for message in messages:
            if isinstance(message, dict):
                items.extend(item for item in message.get("files", []) if isinstance(item, dict))
        return items

    @staticmethod
    def _active_messages(chat: dict[str, Any]) -> list[dict[str, Any]]:
        history = chat.get("history") or {}
        messages = history.get("messages") or {}
        current_id = history.get("currentId")
        result: list[dict[str, Any]] = []
        seen: set[str] = set()
        while current_id and current_id not in seen:
            seen.add(str(current_id))
            message = messages.get(str(current_id))
            if not isinstance(message, dict):
                break
            result.append(message)
            current_id = message.get("parentId")
        return list(reversed(result))

    @staticmethod
    def _prior_messages(messages: Any) -> list[dict[str, Any]]:
        if not isinstance(messages, list):
            return []
        last_user = next(
            (
                index
                for index in range(len(messages) - 1, -1, -1)
                if messages[index].get("role") == "user"
            ),
            len(messages),
        )
        return [message for message in messages[:last_user] if isinstance(message, dict)]

    @classmethod
    def _format_messages(cls, messages: list[dict[str, Any]]) -> str:
        parts: list[str] = []
        for message in messages:
            role = str(message.get("role", "")).lower()
            if role not in {"user", "assistant"}:
                continue
            content = cls._message_text(message.get("content"))
            if content:
                parts.append(f"### {role.title()}\n{content}")
        return "\n\n".join(parts)

    @staticmethod
    def _message_text(content: Any) -> str:
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            return "\n".join(
                str(item.get("text", ""))
                for item in content
                if isinstance(item, dict) and item.get("type") == "text"
            ).strip()
        if content is None:
            return ""
        return json.dumps(content, ensure_ascii=False, default=str)

    def _compact_documents(self, documents: list[dict[str, str]]) -> list[dict[str, str]]:
        if not documents:
            return []
        per_document = max(2_000, self.valves.context_char_limit // len(documents))
        result: list[dict[str, str]] = []
        remaining = self.valves.context_char_limit
        seen: set[str] = set()
        for document in documents:
            text = document["text"].strip()
            digest = hashlib.sha256(text.encode()).hexdigest()
            if not text or digest in seen or remaining <= 0:
                continue
            seen.add(digest)
            allowance = min(per_document, remaining)
            if len(text) > allowance:
                text = (
                    "[Earlier conversation omitted to fit the context limit]\n\n"
                    + text[-(allowance - 62) :]
                )
            copied = {**document, "text": text}
            result.append(copied)
            remaining -= len(text)
        return result

    @staticmethod
    def _authorization(request: Any) -> str | None:
        if request is None:
            return None
        value = request.headers.get("authorization")
        return str(value) if value else None
