from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from asgi_lifespan import LifespanManager

from open_webui_gpt_researcher.api import create_app
from open_webui_gpt_researcher.config import Settings


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'test.sqlite'}",
        artifact_backend="filesystem",
        artifact_path=str(tmp_path / "artifacts"),
        openwebui_url="http://openwebui.invalid",
        openwebui_api_key="test-admin-key",
        service_token="test-service-token",
        signing_secret="test-signing-secret-with-more-than-32-bytes",
        auto_create_schema=True,
        model_profiles={"default": "test-model"},
    )


@pytest.fixture
async def api_client(settings: Settings) -> AsyncIterator[tuple[httpx.AsyncClient, object]]:
    app = create_app(settings)
    async with (
        LifespanManager(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
    ):
        yield client, app


@pytest.fixture
def service_headers() -> dict[str, str]:
    return {
        "X-Research-Service-Token": "test-service-token",
        "X-OpenWebUI-User-Id": "user-1",
        "Idempotency-Key": "idempotency-key-0001",
    }


@pytest.fixture
def job_payload() -> dict[str, object]:
    return {
        "query": "What is the effect of durable queues on reliability?",
        "chat_id": "chat-1",
        "message_id": "message-1",
        "sources": [],
        "budget": {
            "max_input_tokens": 10_000,
            "max_output_tokens": 2_000,
            "max_searches": 5,
            "max_wall_time_seconds": 300,
        },
        "model_profile": "default",
        "report_type": "deep",
        "report_formats": ["markdown", "json"],
    }
