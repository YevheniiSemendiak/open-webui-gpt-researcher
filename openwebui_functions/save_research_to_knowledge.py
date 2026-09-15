"""
title: Save Deep Research to Knowledge
author: Open WebUI GPT Researcher contributors
version: 0.1.0
required_open_webui_version: 0.11.0
requirements: httpx>=0.28,<1
"""

from __future__ import annotations

import json
import re
from typing import Any

import httpx
from pydantic import BaseModel


class Action:
    class Valves(BaseModel):
        service_url: str = "http://open-webui-gpt-researcher:8090"
        service_token: str = ""

    def __init__(self) -> None:
        self.valves = self.Valves()

    async def action(
        self,
        body: dict[str, Any],
        __user__: dict[str, Any],
        __event_call__: Any = None,
        __event_emitter__: Any = None,
    ) -> dict[str, Any]:
        if not self.valves.service_token:
            return {"error": "The administrator has not configured the research service token."}
        match = re.search(r"deep-research-job:([0-9a-f-]{36})", json.dumps(body))
        if match is None:
            return {"error": "This message does not contain a completed deep-research job."}
        if __event_call__ is None:
            return {"error": "An active browser session is required."}
        target = await __event_call__(
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
        target = target.strip()
        request = (
            {"knowledge_id": target[3:].strip()} if target.startswith("id:") else {"name": target}
        )
        try:
            async with httpx.AsyncClient(timeout=120) as client:
                response = await client.post(
                    f"{self.valves.service_url.rstrip('/')}/v1/research-jobs/"
                    f"{match.group(1)}:save-to-knowledge",
                    headers={
                        "X-Research-Service-Token": self.valves.service_token,
                        "X-OpenWebUI-User-Id": str(__user__.get("id", "")),
                    },
                    json=request,
                )
                response.raise_for_status()
                result = response.json()
        except httpx.HTTPStatusError as error:
            return {"error": error.response.text[:1_000]}
        except httpx.HTTPError as error:
            return {"error": f"Research service unavailable: {error}"}
        if __event_emitter__ is not None:
            await __event_emitter__(
                {
                    "type": "notification",
                    "data": {
                        "type": "success",
                        "content": "Research saved to Knowledge.",
                    },
                }
            )
        return result
