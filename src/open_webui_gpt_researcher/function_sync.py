from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import httpx
import structlog

from .config import Settings

log = structlog.get_logger()


@dataclass(frozen=True)
class FunctionSource:
    id: str
    name: str
    filename: str
    description: str


FUNCTION_SOURCES = (
    FunctionSource(
        id="deep_research",
        name="Deep Research",
        filename="deep_research_pipe.py",
        description="Submit durable GPT Researcher jobs from Open WebUI.",
    ),
    FunctionSource(
        id="save_deep_research_to_knowledge",
        name="Deep Research Artifacts",
        filename="save_research_to_knowledge.py",
        description="Attach completed artifacts or save a report to Open WebUI Knowledge.",
    ),
)


class FunctionSync:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.client = httpx.AsyncClient(
            base_url=settings.openwebui_url.rstrip("/"),
            headers={"Authorization": f"Bearer {settings.openwebui_api_key.get_secret_value()}"},
            timeout=120,
        )

    async def close(self) -> None:
        await self.client.aclose()

    async def run(self) -> list[str]:
        response = await self.client.get("/api/v1/functions/export")
        response.raise_for_status()
        existing = {item["id"]: item for item in response.json()}
        changed: list[str] = []
        for source in FUNCTION_SOURCES:
            content = (Path(self.settings.function_sources_path) / source.filename).read_text()
            form: dict[str, object] = {
                "id": source.id,
                "name": source.name,
                "content": content,
                "meta": {"description": source.description},
            }
            current = existing.get(source.id)
            if current is None:
                result = await self._post("/api/v1/functions/create", form)
                changed.append(f"created:{source.id}")
            elif (
                current.get("content") != content
                or current.get("name") != source.name
                or (current.get("meta") or {}).get("description") != source.description
            ):
                result = await self._post(f"/api/v1/functions/id/{source.id}/update", form)
                changed.append(f"updated:{source.id}")
            else:
                result = current

            valves: dict[str, object] = {
                "service_url": self.settings.internal_base_url,
                "service_token": self.settings.service_token.get_secret_value(),
            }
            if source.id == "deep_research":
                valves["public_search_enabled"] = self.settings.public_search_enabled
                valves["openwebui_url"] = self.settings.openwebui_url
                valves["default_models"] = self.settings.resolve_default_models().model_dump()
            else:
                valves["openwebui_url"] = self.settings.openwebui_url
            valve_response = await self.client.get(f"/api/v1/functions/id/{source.id}/valves")
            valve_response.raise_for_status()
            current_valves = valve_response.json() or {}
            if not isinstance(current_valves, dict):
                raise RuntimeError(f"Open WebUI returned invalid Valves for {source.id}")
            desired_valves = {**current_valves, **valves}
            if desired_valves != current_valves:
                await self._post(f"/api/v1/functions/id/{source.id}/valves/update", desired_valves)
                changed.append(f"valves:{source.id}")

            if not bool(result.get("is_active")):
                await self._post(f"/api/v1/functions/id/{source.id}/toggle", {})
                changed.append(f"activated:{source.id}")
        await self._sync_deep_research_model(changed)
        log.info("openwebui.functions_synced", changes=changed)
        return changed

    async def _sync_deep_research_model(self, changed: list[str]) -> None:
        response = await self.client.get("/api/v1/models/model", params={"id": "deep_research"})
        if response.status_code == 404:
            current = None
        else:
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                raise RuntimeError("Open WebUI returned invalid model metadata")
            current = payload
        meta = dict((current or {}).get("meta") or {})
        action_ids = list(meta.get("actionIds") or [])
        if "save_deep_research_to_knowledge" not in action_ids:
            action_ids.append("save_deep_research_to_knowledge")
        meta.update(
            {
                "description": "Durable, multi-source deep research.",
                "actionIds": action_ids,
            }
        )
        form: dict[str, object] = {
            "id": "deep_research",
            "base_model_id": (current or {}).get("base_model_id"),
            "name": (current or {}).get("name") or "Deep Research",
            "meta": meta,
            "params": dict((current or {}).get("params") or {}),
            "access_grants": (current or {}).get("access_grants")
            or [
                {
                    "principal_type": "user",
                    "principal_id": "*",
                    "permission": "read",
                }
            ],
            "is_active": True,
        }
        if current is None:
            await self._post("/api/v1/models/create", form)
            changed.append("created:model:deep_research")
        elif (current.get("meta") or {}).get("actionIds") != action_ids or (
            current.get("meta") or {}
        ).get("description") != meta["description"]:
            await self._post("/api/v1/models/model/update", form)
            changed.append("updated:model:deep_research")

    async def _post(self, path: str, body: dict[str, object]) -> dict[str, object]:
        response = await self.client.post(path, json=body)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise RuntimeError(f"Open WebUI returned an invalid response for {path}")
        return payload
