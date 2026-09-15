from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import boto3


@dataclass(frozen=True)
class StoredArtifact:
    object_key: str
    size: int


class ArtifactStore(Protocol):
    async def put(self, object_key: str, data: bytes, media_type: str) -> StoredArtifact: ...

    async def get(self, object_key: str) -> bytes: ...


def safe_object_key(object_key: str) -> str:
    path = Path(object_key)
    if path.is_absolute() or ".." in path.parts:
        msg = "unsafe artifact object key"
        raise ValueError(msg)
    return path.as_posix()


class FilesystemArtifactStore:
    def __init__(self, root: str) -> None:
        self.root = Path(root).resolve()

    async def put(self, object_key: str, data: bytes, media_type: str) -> StoredArtifact:
        del media_type
        key = safe_object_key(object_key)
        path = self.root / key
        path.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(path.write_bytes, data)
        return StoredArtifact(object_key=key, size=len(data))

    async def get(self, object_key: str) -> bytes:
        path = self.root / safe_object_key(object_key)
        return await asyncio.to_thread(path.read_bytes)


class S3ArtifactStore:
    def __init__(
        self,
        *,
        bucket: str,
        endpoint_url: str | None,
        region: str,
        access_key_id: str,
        secret_access_key: str,
    ) -> None:
        self.bucket = bucket
        self.client = boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            region_name=region,
            aws_access_key_id=access_key_id or None,
            aws_secret_access_key=secret_access_key or None,
        )

    async def put(self, object_key: str, data: bytes, media_type: str) -> StoredArtifact:
        key = safe_object_key(object_key)
        await asyncio.to_thread(
            self.client.put_object,
            Bucket=self.bucket,
            Key=key,
            Body=data,
            ContentType=media_type,
        )
        return StoredArtifact(object_key=key, size=len(data))

    async def get(self, object_key: str) -> bytes:
        key = safe_object_key(object_key)
        response = await asyncio.to_thread(self.client.get_object, Bucket=self.bucket, Key=key)
        return await asyncio.to_thread(response["Body"].read)
