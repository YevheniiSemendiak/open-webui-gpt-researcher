"""
title: Deep Research Artifacts
author: Open WebUI GPT Researcher contributors
version: 0.0.0-dev
required_open_webui_version: 0.11.0
requirements: httpx>=0.28
"""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import quote

import httpx
from pydantic import BaseModel

ACTIONS = [{"id": "save_to_knowledge", "name": "Save report to Knowledge"}]

CREATE_KNOWLEDGE_TARGET = "__create_new_knowledge__"


class Action:
    actions = ACTIONS

    class Valves(BaseModel):
        service_url: str = "http://open-webui-gpt-researcher:8090"
        service_token: str = ""
        openwebui_url: str = "http://open-webui:8080"

    def __init__(self) -> None:
        self.valves = self.Valves()

    async def action(
        self,
        body: dict[str, Any],
        __user__: dict[str, Any],
        __id__: str = "save_to_knowledge",
        __request__: Any = None,
        __event_call__: Any = None,
        __event_emitter__: Any = None,
    ) -> dict[str, Any]:
        del __id__
        if not self.valves.service_token:
            return await self._error(
                "The administrator has not configured the research service token.",
                __event_emitter__,
            )
        authorization = self._authorization(__request__)
        if authorization is None:
            return await self._error(
                "An authenticated Open WebUI request is required.", __event_emitter__
            )
        user_id = str(__user__.get("id", ""))

        try:
            async with httpx.AsyncClient(timeout=120) as client:
                job_id = self._job_id(body)
                if job_id is None:
                    job_id = await self._resolve_job_id(client, body=body, user_id=user_id)
                if job_id is None:
                    return await self._error(
                        "This message is not associated with a deep-research job.",
                        __event_emitter__,
                    )
                existing_files = await self._message_files(
                    client, body=body, authorization=authorization
                )
                if __event_call__ is None:
                    return await self._error(
                        "An active browser session is required.", __event_emitter__
                    )
                result = await self._save_to_knowledge(
                    client,
                    job_id=job_id,
                    existing_files=existing_files,
                    user_id=user_id,
                    authorization=authorization,
                    event_call=__event_call__,
                )
        except httpx.HTTPStatusError as error:
            detail = error.response.text[:1_000]
            return await self._error(
                f"Open WebUI rejected the Knowledge update: {detail}", __event_emitter__
            )
        except httpx.HTTPError as error:
            return await self._error(f"Knowledge update failed: {error}", __event_emitter__)

        if error := result.get("error"):
            return await self._error(str(error), __event_emitter__)

        if __event_emitter__ is not None:
            await __event_emitter__(
                {
                    "type": "notification",
                    "data": {"type": "success", "content": "Research report saved to Knowledge."},
                }
            )
        return result

    @staticmethod
    async def _error(message: str, event_emitter: Any) -> dict[str, str]:
        if event_emitter is not None:
            await event_emitter(
                {
                    "type": "notification",
                    "data": {"type": "error", "content": message},
                }
            )
        return {"error": message}

    async def _save_to_knowledge(
        self,
        client: httpx.AsyncClient,
        *,
        job_id: str,
        existing_files: list[dict[str, Any]],
        user_id: str,
        authorization: str,
        event_call: Any,
    ) -> dict[str, Any]:
        writable_knowledge = await self._writable_knowledge(client, authorization=authorization)
        if writable_knowledge:
            target = await event_call(
                {
                    "type": "input",
                    "data": {
                        "title": "Save research to Knowledge",
                        "message": "Choose a writable Knowledge collection for this report.",
                        "placeholder": "Select a Knowledge collection",
                        "input": {
                            "type": "select",
                            "options": [
                                {
                                    "label": "Create a new Knowledge collection",
                                    "value": CREATE_KNOWLEDGE_TARGET,
                                },
                                *[
                                    {"label": item["name"], "value": item["id"]}
                                    for item in writable_knowledge
                                ],
                            ],
                        },
                    },
                }
            )
        else:
            target = CREATE_KNOWLEDGE_TARGET

        if target == CREATE_KNOWLEDGE_TARGET:
            target = await event_call(
                {
                    "type": "input",
                    "data": {
                        "title": "Create Knowledge collection",
                        "message": "Enter a name for the new private Knowledge collection.",
                        "placeholder": "Research: market landscape",
                    },
                }
            )
            create_knowledge = True
        else:
            create_knowledge = False

        if not isinstance(target, str) or not target.strip():
            return {"error": "Save cancelled."}
        if not create_knowledge and target not in {item["id"] for item in writable_knowledge}:
            return {"error": "The selected Knowledge collection is not writable."}

        report_file_id = self._attached_report_id(existing_files)
        if report_file_id is None:
            content, media_type = await self._fetch_artifact(
                client, job_id=job_id, name="report.md", user_id=user_id
            )
            uploaded = await self._upload_to_openwebui(
                client,
                name="report.md",
                content=content,
                media_type=media_type,
                authorization=authorization,
                process=True,
                job_id=job_id,
            )
            report_file_id = str(uploaded["id"])

        target = target.strip()
        headers = {"Authorization": authorization}
        if create_knowledge:
            response = await client.post(
                f"{self.valves.openwebui_url.rstrip('/')}/api/v1/knowledge/create",
                headers=headers,
                json={
                    "name": target,
                    "description": "Created from a deep-research report",
                    "access_grants": [],
                },
            )
            response.raise_for_status()
            knowledge_id = str(response.json()["id"])
        else:
            knowledge_id = target

        response = await client.post(
            f"{self.valves.openwebui_url.rstrip('/')}/api/v1/knowledge/{knowledge_id}/file/add",
            headers=headers,
            json={"file_id": report_file_id},
        )
        response.raise_for_status()
        return {"knowledge_id": knowledge_id, "file_id": report_file_id}

    async def _writable_knowledge(
        self,
        client: httpx.AsyncClient,
        *,
        authorization: str,
    ) -> list[dict[str, str]]:
        headers = {"Authorization": authorization}
        writable: list[dict[str, str]] = []
        page = 1
        seen = 0
        while True:
            response = await client.get(
                f"{self.valves.openwebui_url.rstrip('/')}/api/v1/knowledge/",
                headers=headers,
                params={"page": page},
            )
            response.raise_for_status()
            payload = response.json()
            items = payload.get("items", []) if isinstance(payload, dict) else []
            seen += len(items)
            for item in items:
                if not isinstance(item, dict) or item.get("write_access") is not True:
                    continue
                meta = item.get("meta")
                if isinstance(meta, dict) and meta.get("source") == "external":
                    continue
                knowledge_id = item.get("id")
                name = item.get("name")
                if knowledge_id and name:
                    writable.append({"id": str(knowledge_id), "name": str(name)})

            total = payload.get("total", len(items)) if isinstance(payload, dict) else len(items)
            if not items or seen >= total:
                break
            page += 1

        return writable

    async def _resolve_job_id(
        self, client: httpx.AsyncClient, *, body: dict[str, Any], user_id: str
    ) -> str | None:
        chat_id = body.get("chat_id")
        message_id = body.get("id")
        if not chat_id or not message_id:
            return None
        response = await client.get(
            f"{self.valves.service_url.rstrip('/')}/v1/research-jobs/resolve",
            params={"chat_id": str(chat_id), "message_id": str(message_id)},
            headers=self._service_headers(user_id),
        )
        if response.status_code == 404:
            return None
        response.raise_for_status()
        payload = response.json()
        return str(payload["id"]) if payload.get("state") == "succeeded" else None

    async def _message_files(
        self,
        client: httpx.AsyncClient,
        *,
        body: dict[str, Any],
        authorization: str,
    ) -> list[dict[str, Any]]:
        chat_id = body.get("chat_id")
        message_id = body.get("id")
        if not chat_id or not message_id:
            return []
        response = await client.get(
            f"{self.valves.openwebui_url.rstrip('/')}/api/v1/chats/{quote(str(chat_id), safe='')}",
            headers={"Authorization": authorization},
        )
        response.raise_for_status()
        payload = response.json()
        messages = ((payload.get("chat") or {}).get("history") or {}).get("messages") or {}
        message = messages.get(str(message_id)) or {}
        files = message.get("files") or []
        return [item for item in files if isinstance(item, dict)]

    async def _fetch_artifact(
        self,
        client: httpx.AsyncClient,
        *,
        job_id: str,
        name: str,
        user_id: str,
    ) -> tuple[bytes, str]:
        response = await client.get(
            f"{self.valves.service_url.rstrip('/')}/v1/research-jobs/{job_id}/artifacts/"
            f"{quote(name, safe='')}/content",
            headers=self._service_headers(user_id),
        )
        response.raise_for_status()
        return response.content, response.headers.get("content-type", "application/octet-stream")

    async def _upload_to_openwebui(
        self,
        client: httpx.AsyncClient,
        *,
        name: str,
        content: bytes,
        media_type: str,
        authorization: str,
        process: bool,
        job_id: str,
    ) -> dict[str, Any]:
        response = await client.post(
            f"{self.valves.openwebui_url.rstrip('/')}/api/v1/files/",
            params={"process": str(process).lower()},
            headers={"Authorization": authorization},
            files={"file": (name, content, media_type)},
            data={"metadata": json.dumps({"deep_research_job_id": job_id})},
        )
        response.raise_for_status()
        payload = response.json()
        metadata = payload.get("meta") or {}
        file_id = str(payload["id"])
        await self._set_preview_content(
            client,
            file_id=file_id,
            content=content,
            media_type=media_type,
            authorization=authorization,
        )
        return {
            "type": "file",
            "id": file_id,
            "url": file_id,
            "name": str(metadata.get("name") or payload.get("filename") or name),
            "size": int(metadata.get("size") or len(content)),
            "content_type": str(metadata.get("content_type") or media_type),
        }

    async def _set_preview_content(
        self,
        client: httpx.AsyncClient,
        *,
        file_id: str,
        content: bytes,
        media_type: str,
        authorization: str,
    ) -> None:
        if not (media_type.startswith("text/") or media_type.startswith("application/json")):
            return
        response = await client.post(
            f"{self.valves.openwebui_url.rstrip('/')}/api/v1/files/"
            f"{quote(file_id, safe='')}/data/content/update",
            headers={"Authorization": authorization},
            json={"content": content.decode("utf-8")},
        )
        response.raise_for_status()

    def _service_headers(self, user_id: str) -> dict[str, str]:
        return {
            "X-Research-Service-Token": self.valves.service_token,
            "X-OpenWebUI-User-Id": user_id,
        }

    @staticmethod
    def _authorization(request: Any) -> str | None:
        if request is None:
            return None
        value = request.headers.get("authorization")
        return str(value) if value else None

    @staticmethod
    def _job_id(body: dict[str, Any]) -> str | None:
        serialized = json.dumps(body)
        match = re.search(r'"job_id"\s*:\s*"([0-9a-f-]{36})"', serialized)
        if match is None:
            match = re.search(r"deep-research-job:([0-9a-f-]{36})", serialized)
        return match.group(1) if match is not None else None

    @staticmethod
    def _attached_report_id(files: list[dict[str, Any]]) -> str | None:
        for item in files:
            if isinstance(item, dict) and item.get("name") == "report.md" and item.get("id"):
                return str(item["id"])
        return None
