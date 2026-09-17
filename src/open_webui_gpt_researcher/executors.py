from __future__ import annotations

import asyncio
import enum
import os
import sys
from contextlib import suppress
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import UUID

from kubernetes_asyncio import client, config
from kubernetes_asyncio.client.exceptions import ApiException

from .config import Settings


def _kubernetes_model(data: dict[str, object], model: type[Any]) -> Any:
    """Convert native Kubernetes JSON fields into the generated client model."""
    api_client = client.ApiClient.__new__(client.ApiClient)
    return api_client._ApiClient__deserialize(data, model)  # type: ignore[attr-defined]


class DispatchStatus(enum.StrEnum):
    MISSING = "missing"
    ACTIVE = "active"
    EXITED = "exited"


class Executor(Protocol):
    async def submit(self, *, job_id: UUID, runner_token: str, attempt: int) -> None: ...

    async def inspect(self, *, job_id: UUID, attempt: int) -> DispatchStatus: ...

    async def stop(self, *, job_id: UUID, attempt: int) -> None: ...


@dataclass
class LocalProcessExecutor:
    settings: Settings

    def __post_init__(self) -> None:
        self._processes: dict[tuple[UUID, int], asyncio.subprocess.Process] = {}

    async def submit(self, *, job_id: UUID, runner_token: str, attempt: int) -> None:
        environment = dict(os.environ)
        for secret_name in (
            "DATABASE_URL",
            "OPENWEBUI_API_KEY",
            "S3_ACCESS_KEY_ID",
            "S3_SECRET_ACCESS_KEY",
            "SERVICE_TOKEN",
            "SIGNING_SECRET",
        ):
            environment.pop(secret_name, None)
        environment.update(
            {
                "JOB_ID": str(job_id),
                "RUNNER_TOKEN": runner_token,
                "INTERNAL_BASE_URL": self.settings.internal_base_url,
            }
        )
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "open_webui_gpt_researcher.cli",
            "runner",
            env=environment,
        )
        self._processes[(job_id, attempt)] = process

    async def inspect(self, *, job_id: UUID, attempt: int) -> DispatchStatus:
        process = self._processes.get((job_id, attempt))
        if process is None:
            return DispatchStatus.MISSING
        if process.returncode is None:
            return DispatchStatus.ACTIVE
        self._processes.pop((job_id, attempt), None)
        return DispatchStatus.EXITED

    async def stop(self, *, job_id: UUID, attempt: int) -> None:
        process = self._processes.pop((job_id, attempt), None)
        if process is None or process.returncode is not None:
            return
        with suppress(ProcessLookupError):
            process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=10)
        except TimeoutError:
            with suppress(ProcessLookupError):
                process.kill()
            await process.wait()


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
            raise RuntimeError(f"owner Deployment {owner_name!r} has no UID")
        self._owner_reference = client.V1OwnerReference(
            api_version="apps/v1",
            kind="Deployment",
            name=owner_name,
            uid=metadata.uid,
            controller=False,
            block_owner_deletion=False,
        )
        return self._owner_reference

    @staticmethod
    def _job_name(job_id: UUID, attempt: int) -> str:
        return f"deep-research-{job_id}-{attempt}"

    async def submit(self, *, job_id: UUID, runner_token: str, attempt: int) -> None:
        await self._configure()
        settings = self.settings
        name = self._job_name(job_id, attempt)
        owner_reference = await self._get_owner_reference()
        labels = {
            **settings.runner_pod_labels,
            "app.kubernetes.io/name": "open-webui-gpt-researcher",
            "app.kubernetes.io/component": "runner",
            "job-id": str(job_id),
            "job-attempt": str(attempt),
        }
        environment = [
            client.V1EnvVar(
                name=key,
                value=str(value).lower() if isinstance(value, bool) else str(value),
            )
            for key, value in settings.runner_env.items()
        ]
        environment.extend(
            [
                client.V1EnvVar(name="JOB_ID", value=str(job_id)),
                client.V1EnvVar(name="RUNNER_TOKEN", value=runner_token),
                client.V1EnvVar(name="INTERNAL_BASE_URL", value=settings.internal_base_url),
            ]
        )
        env_from = [
            _kubernetes_model(item, client.V1EnvFromSource) for item in settings.runner_env_from
        ]
        volume_mounts = [client.V1VolumeMount(name="tmp", mount_path="/tmp")]  # noqa: S108
        volume_mounts.extend(
            _kubernetes_model(item, client.V1VolumeMount)
            for item in settings.runner_extra_volume_mounts
        )
        volumes = [client.V1Volume(name="tmp", empty_dir=client.V1EmptyDirVolumeSource())]
        volumes.extend(
            _kubernetes_model(item, client.V1Volume) for item in settings.runner_extra_volumes
        )
        container = client.V1Container(
            name="runner",
            image=settings.runner_image,
            image_pull_policy=settings.runner_image_pull_policy,
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
            security_context=_kubernetes_model(
                settings.runner_container_security_context,
                client.V1SecurityContext,
            ),
            volume_mounts=volume_mounts,
        )
        pod_spec = client.V1PodSpec(
            restart_policy="Never",
            service_account_name=settings.runner_service_account,
            automount_service_account_token=False,
            security_context=_kubernetes_model(
                settings.runner_pod_security_context,
                client.V1PodSecurityContext,
            ),
            image_pull_secrets=[
                _kubernetes_model(item, client.V1LocalObjectReference)
                for item in settings.runner_image_pull_secrets
            ],
            node_selector=settings.runner_node_selector,
            tolerations=[
                _kubernetes_model(item, client.V1Toleration) for item in settings.runner_tolerations
            ],
            affinity=(
                _kubernetes_model(settings.runner_affinity, client.V1Affinity)
                if settings.runner_affinity
                else None
            ),
            containers=[container],
            volumes=volumes,
        )
        job = client.V1Job(
            metadata=client.V1ObjectMeta(
                name=name,
                labels=labels,
                annotations=settings.runner_pod_annotations,
                owner_references=[owner_reference] if owner_reference else None,
            ),
            spec=client.V1JobSpec(
                backoff_limit=settings.runner_backoff_limit,
                active_deadline_seconds=settings.runner_active_deadline_seconds,
                ttl_seconds_after_finished=settings.runner_ttl_seconds_after_finished,
                template=client.V1PodTemplateSpec(
                    metadata=client.V1ObjectMeta(
                        labels=labels,
                        annotations=settings.runner_pod_annotations,
                    ),
                    spec=pod_spec,
                ),
            ),
        )
        api = client.BatchV1Api()
        try:
            await api.create_namespaced_job(namespace=settings.runner_namespace, body=job)
        except ApiException as error:
            if error.status != 409:
                raise

    async def inspect(self, *, job_id: UUID, attempt: int) -> DispatchStatus:
        await self._configure()
        try:
            job = await client.BatchV1Api().read_namespaced_job(
                name=self._job_name(job_id, attempt),
                namespace=self.settings.runner_namespace,
            )
        except ApiException as error:
            if error.status == 404:
                return DispatchStatus.MISSING
            raise
        status = job.status
        if status is not None and ((status.failed or 0) > 0 or (status.succeeded or 0) > 0):
            return DispatchStatus.EXITED
        return DispatchStatus.ACTIVE

    async def stop(self, *, job_id: UUID, attempt: int) -> None:
        await self._configure()
        try:
            await client.BatchV1Api().delete_namespaced_job(
                name=self._job_name(job_id, attempt),
                namespace=self.settings.runner_namespace,
                body=client.V1DeleteOptions(propagation_policy="Background"),
            )
        except ApiException as error:
            if error.status != 404:
                raise


def make_executor(settings: Settings) -> Executor:
    if settings.mode == "local":
        return LocalProcessExecutor(settings)
    return KubernetesJobExecutor(settings)
