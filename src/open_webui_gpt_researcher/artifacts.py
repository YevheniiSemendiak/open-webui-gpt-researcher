from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

import boto3


@dataclass(frozen=True)
class StoredArtifact:
    object_key: str
    size: int


@dataclass(frozen=True)
class StoredObject:
    object_key: str
    last_modified: datetime


class ArtifactStore(Protocol):
    async def put(self, object_key: str, data: bytes, media_type: str) -> StoredArtifact: ...

    async def get(self, object_key: str) -> bytes: ...

    async def delete(self, object_key: str) -> None: ...

    async def list_objects(self, prefix: str) -> list[StoredObject]: ...


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

    async def delete(self, object_key: str) -> None:
        path = self.root / safe_object_key(object_key)

        def remove() -> None:
            path.unlink(missing_ok=True)
            parent = path.parent
            while parent != self.root:
                try:
                    parent.rmdir()
                except OSError:
                    break
                parent = parent.parent

        await asyncio.to_thread(remove)

    async def list_objects(self, prefix: str) -> list[StoredObject]:
        safe_prefix = safe_object_key(prefix)

        def scan() -> list[StoredObject]:
            root = self.root / safe_prefix
            if not root.exists():
                return []
            return [
                StoredObject(
                    object_key=path.relative_to(self.root).as_posix(),
                    last_modified=datetime.fromtimestamp(path.stat().st_mtime, tz=UTC),
                )
                for path in root.rglob("*")
                if path.is_file()
            ]

        return await asyncio.to_thread(scan)


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

    async def delete(self, object_key: str) -> None:
        key = safe_object_key(object_key)
        await asyncio.to_thread(self.client.delete_object, Bucket=self.bucket, Key=key)

    async def list_objects(self, prefix: str) -> list[StoredObject]:
        safe_prefix = safe_object_key(prefix).rstrip("/") + "/"

        def scan() -> list[StoredObject]:
            found: list[StoredObject] = []
            continuation_token: str | None = None
            while True:
                if continuation_token:
                    response = self.client.list_objects_v2(
                        Bucket=self.bucket,
                        Prefix=safe_prefix,
                        ContinuationToken=continuation_token,
                    )
                else:
                    response = self.client.list_objects_v2(
                        Bucket=self.bucket,
                        Prefix=safe_prefix,
                    )
                for item in response.get("Contents", []):
                    key = str(item["Key"])
                    modified = item["LastModified"]
                    if modified.tzinfo is None:
                        modified = modified.replace(tzinfo=UTC)
                    found.append(StoredObject(object_key=key, last_modified=modified))
                if not response.get("IsTruncated"):
                    return found
                continuation_token = str(response["NextContinuationToken"])

        return await asyncio.to_thread(scan)
