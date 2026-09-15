from __future__ import annotations

import asyncio
import os
import sys
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from kubernetes_asyncio import client, config

from .config import Settings


class Executor(Protocol):
    async def submit(self, *, job_id: UUID, runner_token: str) -> None: ...


@dataclass
class LocalProcessExecutor:
    settings: Settings

    async def submit(self, *, job_id: UUID, runner_token: str) -> None:
        environment = dict(os.environ)
        for secret_name in (
            "RESEARCH_DATABASE_URL",
            "RESEARCH_OPENWEBUI_API_KEY",
            "RESEARCH_S3_ACCESS_KEY_ID",
            "RESEARCH_S3_SECRET_ACCESS_KEY",
            "RESEARCH_SERVICE_TOKEN",
            "RESEARCH_SIGNING_SECRET",
        ):
            environment.pop(secret_name, None)
        environment.update(
            {
                "RESEARCH_JOB_ID": str(job_id),
                "RESEARCH_RUNNER_TOKEN": runner_token,
                "RESEARCH_INTERNAL_BASE_URL": self.settings.internal_base_url,
            }
        )
        await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "open_webui_gpt_researcher.cli",
            "runner",
            env=environment,
        )


class KubernetesJobExecutor:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._configured = False
        self._owner_reference: client.V1OwnerReference | None = None

    async def _configure(self) -> None:
        if self._configured:
            return
        try:
            config.load_incluster_config()  # type: ignore[no-untyped-call]
        except config.ConfigException:
            await config.load_kube_config()
        self._configured = True

    async def _get_owner_reference(self) -> client.V1OwnerReference | None:
        owner_name = self.settings.runner_owner_deployment
        if not owner_name:
            return None
        if self._owner_reference is not None:
            return self._owner_reference
        deployment = await client.AppsV1Api().read_namespaced_deployment(
            name=owner_name,
            namespace=self.settings.runner_namespace,
        )
        metadata = deployment.metadata
        if metadata is None or not metadata.uid:
            raise RuntimeError(f"controller Deployment {owner_name!r} has no UID")
        self._owner_reference = client.V1OwnerReference(
            api_version="apps/v1",
            kind="Deployment",
            name=owner_name,
            uid=metadata.uid,
            controller=False,
            block_owner_deletion=False,
        )
        return self._owner_reference

    async def submit(self, *, job_id: UUID, runner_token: str) -> None:
        await self._configure()
        settings = self.settings
        name = f"deep-research-{str(job_id)[:8]}"
        owner_reference = await self._get_owner_reference()
        labels = {
            "app.kubernetes.io/name": "open-webui-gpt-researcher",
            "app.kubernetes.io/component": "runner",
            "job-id": str(job_id),
        }
        environment = [
            client.V1EnvVar(name="RESEARCH_JOB_ID", value=str(job_id)),
            client.V1EnvVar(name="RESEARCH_RUNNER_TOKEN", value=runner_token),
            client.V1EnvVar(name="RESEARCH_INTERNAL_BASE_URL", value=settings.internal_base_url),
            client.V1EnvVar(name="RESEARCH_ENGINE", value=settings.engine),
            client.V1EnvVar(
                name="RESEARCH_PUBLIC_SEARCH_ENABLED",
                value=str(settings.public_search_enabled).lower(),
            ),
            client.V1EnvVar(name="RESEARCH_MODEL_ROUTE", value=settings.model_route),
            client.V1EnvVar(
                name="RESEARCH_MODEL_PROFILES",
                value=str(settings.model_profiles).replace("'", '"'),
            ),
            client.V1EnvVar(name="RESEARCH_EMBEDDING_MODEL", value=settings.embedding_model),
        ]
        env_from = []
        if settings.runner_extra_env_secret:
            env_from.append(
                client.V1EnvFromSource(
                    secret_ref=client.V1SecretEnvSource(name=settings.runner_extra_env_secret)
                )
            )
        container = client.V1Container(
            name="runner",
            image=settings.runner_image,
            image_pull_policy="IfNotPresent",
            args=["runner"],
            env=environment,
            env_from=env_from,
            resources=client.V1ResourceRequirements(
                requests={
                    "cpu": settings.runner_cpu_request,
                    "memory": settings.runner_memory_request,
                },
                limits={
                    "cpu": settings.runner_cpu_limit,
                    "memory": settings.runner_memory_limit,
                },
            ),
            security_context=client.V1SecurityContext(
                allow_privilege_escalation=False,
                capabilities=client.V1Capabilities(drop=["ALL"]),
                read_only_root_filesystem=True,
                run_as_non_root=True,
                run_as_user=10001,
            ),
            volume_mounts=[client.V1VolumeMount(name="tmp", mount_path="/tmp")],  # noqa: S108
        )
        pod_spec = client.V1PodSpec(
            restart_policy="Never",
            service_account_name=settings.runner_service_account,
            automount_service_account_token=False,
            security_context=client.V1PodSecurityContext(
                run_as_non_root=True, seccomp_profile=client.V1SeccompProfile(type="RuntimeDefault")
            ),
            containers=[container],
            volumes=[client.V1Volume(name="tmp", empty_dir=client.V1EmptyDirVolumeSource())],
        )
        job = client.V1Job(
            metadata=client.V1ObjectMeta(
                name=name,
                labels=labels,
                owner_references=[owner_reference] if owner_reference else None,
            ),
            spec=client.V1JobSpec(
                backoff_limit=0,
                active_deadline_seconds=settings.runner_active_deadline_seconds,
                ttl_seconds_after_finished=settings.runner_ttl_seconds_after_finished,
                template=client.V1PodTemplateSpec(
                    metadata=client.V1ObjectMeta(labels=labels), spec=pod_spec
                ),
            ),
        )
        api = client.BatchV1Api()
        await api.create_namespaced_job(namespace=settings.runner_namespace, body=job)


def make_executor(settings: Settings) -> Executor:
    if settings.executor == "local":
        return LocalProcessExecutor(settings)
    return KubernetesJobExecutor(settings)
