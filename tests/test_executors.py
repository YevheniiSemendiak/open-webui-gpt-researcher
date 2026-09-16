from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

from kubernetes_asyncio.client.exceptions import ApiException

from open_webui_gpt_researcher.config import Settings
from open_webui_gpt_researcher.executors import (
    DispatchStatus,
    KubernetesJobExecutor,
    LocalProcessExecutor,
    make_executor,
)


async def test_local_executor_passes_only_job_runtime_values(monkeypatch: Any) -> None:
    process = SimpleNamespace(returncode=None)
    create = AsyncMock(return_value=process)
    monkeypatch.setattr("asyncio.create_subprocess_exec", create)
    monkeypatch.setenv("OPENWEBUI_API_KEY", "must-not-leak")
    monkeypatch.setenv("DATABASE_URL", "must-not-leak")
    settings = Settings(internal_base_url="http://api")
    executor = LocalProcessExecutor(settings)
    job_id = uuid4()
    await executor.submit(job_id=job_id, runner_token="job-token", attempt=2)
    environment = create.await_args.kwargs["env"]
    assert environment["JOB_ID"] == str(job_id)
    assert environment["RUNNER_TOKEN"] == "job-token"
    assert "OPENWEBUI_API_KEY" not in environment
    assert "DATABASE_URL" not in environment
    assert await executor.inspect(job_id=job_id, attempt=2) == DispatchStatus.ACTIVE
    process.returncode = 0
    assert await executor.inspect(job_id=job_id, attempt=2) == DispatchStatus.EXITED
    assert await executor.inspect(job_id=job_id, attempt=2) == DispatchStatus.MISSING
    assert isinstance(make_executor(settings), LocalProcessExecutor)


async def test_kubernetes_executor_builds_hardened_job(monkeypatch: Any) -> None:
    submitted: dict[str, Any] = {}

    class FakeBatchApi:
        async def create_namespaced_job(self, *, namespace: str, body: Any) -> None:
            submitted["namespace"] = namespace
            submitted["job"] = body

        async def read_namespaced_job(self, *, name: str, namespace: str) -> Any:
            submitted["read"] = (namespace, name)
            return submitted["job"]

    class FakeAppsApi:
        async def read_namespaced_deployment(self, *, name: str, namespace: str) -> Any:
            submitted["owner_lookup"] = (namespace, name)
            return type(
                "Deployment",
                (),
                {"metadata": type("Metadata", (), {"uid": "controller-uid"})()},
            )()

    settings = Settings(
        mode="k8s",
        runner_namespace="research",
        runner_image="registry/research:v1",
        runner_owner_deployment="research-controller",
        runner_extra_env_secret="provider-credentials",
        retriever="searx",
        scraper="nodriver",
        searx_url="http://searxng:8080",
    )
    executor = KubernetesJobExecutor(settings)
    executor._configured = True
    monkeypatch.setattr("open_webui_gpt_researcher.executors.client.BatchV1Api", FakeBatchApi)
    monkeypatch.setattr("open_webui_gpt_researcher.executors.client.AppsV1Api", FakeAppsApi)
    job_id = uuid4()
    await executor.submit(job_id=job_id, runner_token="scoped-token", attempt=3)
    job = submitted["job"]
    container = job.spec.template.spec.containers[0]
    assert submitted["namespace"] == "research"
    assert job.spec.backoff_limit == 0
    assert job.spec.template.spec.automount_service_account_token is False
    assert submitted["owner_lookup"] == ("research", "research-controller")
    assert job.metadata.owner_references[0].kind == "Deployment"
    assert job.metadata.owner_references[0].uid == "controller-uid"
    assert job.spec.template.metadata.labels["app.kubernetes.io/component"] == "runner"
    assert container.security_context.read_only_root_filesystem is True
    env_names = {item.name for item in container.env}
    assert "OPENWEBUI_API_KEY" not in env_names
    env = {item.name: item.value for item in container.env}
    assert env["RETRIEVER"] == "searx"
    assert env["SCRAPER"] == "nodriver"
    assert env["SEARX_URL"] == "http://searxng:8080"
    assert job.metadata.labels["job-attempt"] == "3"
    assert job.metadata.name.endswith("-3")
    assert await executor.inspect(job_id=job_id, attempt=3) == DispatchStatus.ACTIVE
    job.status = type("Status", (), {"failed": 0, "succeeded": 1})()
    assert await executor.inspect(job_id=job_id, attempt=3) == DispatchStatus.EXITED
    assert container.env_from[0].secret_ref.name == "provider-credentials"
    assert isinstance(make_executor(settings), KubernetesJobExecutor)


async def test_kubernetes_executor_treats_conflict_as_idempotent_and_detects_missing(
    monkeypatch: Any,
) -> None:
    class FakeBatchApi:
        async def create_namespaced_job(self, *, namespace: str, body: Any) -> None:
            del namespace, body
            raise ApiException(status=409)

        async def read_namespaced_job(self, *, name: str, namespace: str) -> Any:
            del name, namespace
            raise ApiException(status=404)

    settings = Settings(mode="k8s", runner_namespace="research")
    executor = KubernetesJobExecutor(settings)
    executor._configured = True
    monkeypatch.setattr("open_webui_gpt_researcher.executors.client.BatchV1Api", FakeBatchApi)
    job_id = uuid4()
    await executor.submit(job_id=job_id, runner_token="token", attempt=4)
    assert await executor.inspect(job_id=job_id, attempt=4) == DispatchStatus.MISSING
