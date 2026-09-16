"""
title: Deep Research Artifacts
author: Open WebUI GPT Researcher contributors
version: 0.2.0
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

ACTIONS = [
    {"id": "attach_artifacts", "name": "Attach research files"},
    {"id": "save_to_knowledge", "name": "Save report to Knowledge"},
]


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
        if not self.valves.service_token:
            return {"error": "The administrator has not configured the research service token."}
        authorization = self._authorization(__request__)
        if authorization is None:
            return {"error": "An authenticated Open WebUI request is required."}
        user_id = str(__user__.get("id", ""))

        try:
            async with httpx.AsyncClient(timeout=120) as client:
                job_id = self._job_id(body)
                if job_id is None:
                    job_id = await self._resolve_job_id(client, body=body, user_id=user_id)
                if job_id is None:
                    return {"error": "This message is not associated with a deep-research job."}
                existing_files = await self._message_files(
                    client, body=body, authorization=authorization
                )
                if __id__ == "attach_artifacts":
                    result = await self._attach_artifacts(
                        client,
                        job_id=job_id,
                        existing_files=existing_files,
                        user_id=user_id,
                        authorization=authorization,
                        event_emitter=__event_emitter__,
                    )
                    message = "Research files attached to this message."
                else:
                    if __event_call__ is None:
                        return {"error": "An active browser session is required."}
                    result = await self._save_to_knowledge(
                        client,
                        job_id=job_id,
                        existing_files=existing_files,
                        user_id=user_id,
                        authorization=authorization,
                        event_call=__event_call__,
                    )
                    message = "Research report saved to Knowledge."
        except httpx.HTTPStatusError as error:
            return {"error": error.response.text[:1_000]}
        except httpx.HTTPError as error:
            return {"error": f"Artifact materialization failed: {error}"}

        if __event_emitter__ is not None:
            await __event_emitter__(
                {
                    "type": "notification",
                    "data": {"type": "success", "content": message},
                }
            )
        return result

    async def _attach_artifacts(
        self,
        client: httpx.AsyncClient,
        *,
        job_id: str,
        existing_files: list[dict[str, Any]],
        user_id: str,
        authorization: str,
        event_emitter: Any,
    ) -> dict[str, Any]:
        manifest = await self._manifest(client, job_id=job_id, user_id=user_id)
        existing_names = {
            str(item.get("name"))
            for item in existing_files
            if isinstance(item, dict) and item.get("name")
        }
        existing_by_name = {
            str(item.get("name")): item
            for item in existing_files
            if isinstance(item, dict) and item.get("name") and item.get("id")
        }
        uploaded: list[dict[str, Any]] = []
        for artifact in manifest:
            name = str(artifact["name"])
            if name in existing_names:
                existing = existing_by_name.get(name)
                if existing is not None and not await self._has_preview_content(
                    client,
                    file_id=str(existing["id"]),
                    authorization=authorization,
                ):
                    content, media_type = await self._fetch_artifact(
                        client, job_id=job_id, name=name, user_id=user_id
                    )
                    await self._set_preview_content(
                        client,
                        file_id=str(existing["id"]),
                        content=content,
                        media_type=media_type,
                        authorization=authorization,
                    )
                continue
            content, media_type = await self._fetch_artifact(
                client, job_id=job_id, name=name, user_id=user_id
            )
            uploaded.append(
                await self._upload_to_openwebui(
                    client,
                    name=name,
                    content=content,
                    media_type=media_type,
                    authorization=authorization,
                    process=False,
                    job_id=job_id,
                )
            )

        if uploaded and event_emitter is not None:
            await event_emitter({"type": "files", "data": {"files": uploaded}})
        return {"job_id": job_id, "files": uploaded, "already_attached": not uploaded}

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
        target = await event_call(
            {
                "type": "input",
                "data": {
                    "title": "Save research to Knowledge",
                    "message": (
                        "Enter a name to create a new Knowledge collection. To append to an "
                        "existing collection, enter `id:<knowledge-id>`."
                    ),
                    "placeholder": "Research: market landscape",
                },
            }
        )
        if not isinstance(target, str) or not target.strip():
            return {"error": "Save cancelled."}

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
        if target.startswith("id:"):
            knowledge_id = target[3:].strip()
            if not knowledge_id:
                return {"error": "Knowledge id cannot be empty."}
        else:
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

        response = await client.post(
            f"{self.valves.openwebui_url.rstrip('/')}/api/v1/knowledge/{knowledge_id}/file/add",
            headers=headers,
            json={"file_id": report_file_id},
        )
        response.raise_for_status()
        return {"knowledge_id": knowledge_id, "file_id": report_file_id}

    async def _manifest(
        self, client: httpx.AsyncClient, *, job_id: str, user_id: str
    ) -> list[dict[str, Any]]:
        response = await client.get(
            f"{self.valves.service_url.rstrip('/')}/v1/research-jobs/{job_id}/artifacts",
            headers=self._service_headers(user_id),
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, list):
            raise httpx.HTTPError("Research service returned an invalid artifact manifest")
        return [item for item in payload if isinstance(item, dict) and item.get("name")]

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

    async def _has_preview_content(
        self,
        client: httpx.AsyncClient,
        *,
        file_id: str,
        authorization: str,
    ) -> bool:
        response = await client.get(
            f"{self.valves.openwebui_url.rstrip('/')}/api/v1/files/{quote(file_id, safe='')}",
            headers={"Authorization": authorization},
        )
        response.raise_for_status()
        payload = response.json()
        return bool((payload.get("data") or {}).get("content"))

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
