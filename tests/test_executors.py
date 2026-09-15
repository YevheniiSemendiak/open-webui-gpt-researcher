from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

from open_webui_gpt_researcher.config import Settings
from open_webui_gpt_researcher.executors import (
    KubernetesJobExecutor,
    LocalProcessExecutor,
    make_executor,
)


async def test_local_executor_passes_only_job_runtime_values(monkeypatch: Any) -> None:
    create = AsyncMock()
    monkeypatch.setattr("asyncio.create_subprocess_exec", create)
    monkeypatch.setenv("RESEARCH_OPENWEBUI_API_KEY", "must-not-leak")
    monkeypatch.setenv("RESEARCH_DATABASE_URL", "must-not-leak")
    settings = Settings(internal_base_url="http://api")
    executor = LocalProcessExecutor(settings)
    job_id = uuid4()
    await executor.submit(job_id=job_id, runner_token="job-token")
    environment = create.await_args.kwargs["env"]
    assert environment["RESEARCH_JOB_ID"] == str(job_id)
    assert environment["RESEARCH_RUNNER_TOKEN"] == "job-token"
    assert "RESEARCH_OPENWEBUI_API_KEY" not in environment
    assert "RESEARCH_DATABASE_URL" not in environment
    assert isinstance(make_executor(settings), LocalProcessExecutor)


async def test_kubernetes_executor_builds_hardened_job(monkeypatch: Any) -> None:
    submitted: dict[str, Any] = {}

    class FakeBatchApi:
        async def create_namespaced_job(self, *, namespace: str, body: Any) -> None:
            submitted["namespace"] = namespace
            submitted["job"] = body

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
    )
    executor = KubernetesJobExecutor(settings)
    executor._configured = True
    monkeypatch.setattr("open_webui_gpt_researcher.executors.client.BatchV1Api", FakeBatchApi)
    monkeypatch.setattr("open_webui_gpt_researcher.executors.client.AppsV1Api", FakeAppsApi)
    await executor.submit(job_id=uuid4(), runner_token="scoped-token")
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
    assert "RESEARCH_OPENWEBUI_API_KEY" not in env_names
    assert container.env_from[0].secret_ref.name == "provider-credentials"
    assert isinstance(make_executor(settings), KubernetesJobExecutor)
