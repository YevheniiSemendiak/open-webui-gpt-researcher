from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from open_webui_gpt_researcher.artifacts import (
    FilesystemArtifactStore,
    S3ArtifactStore,
    safe_object_key,
)


async def test_filesystem_artifact_roundtrip(tmp_path: Path) -> None:
    store = FilesystemArtifactStore(str(tmp_path))
    stored = await store.put("jobs/id/report.md", b"report", "text/markdown")
    assert stored.size == 6
    assert await store.get(stored.object_key) == b"report"
    with pytest.raises(ValueError, match="unsafe"):
        safe_object_key("../escape")


async def test_s3_artifact_roundtrip(monkeypatch: pytest.MonkeyPatch) -> None:
    class Body:
        def read(self) -> bytes:
            return b"stored"

    class Client:
        def __init__(self) -> None:
            self.put: dict[str, object] = {}

        def put_object(self, **kwargs: object) -> None:
            self.put = kwargs

        def get_object(self, **kwargs: object) -> dict[str, object]:
            assert kwargs["Key"] == "jobs/id/report.md"
            return {"Body": Body()}

        def delete_object(self, **kwargs: object) -> None:
            self.deleted = kwargs

        def list_objects_v2(self, **kwargs: object) -> dict[str, object]:
            assert kwargs["Prefix"] == "jobs/"
            return {
                "Contents": [
                    {
                        "Key": "jobs/id/report.md",
                        "LastModified": datetime(2025, 1, 1, tzinfo=UTC),
                    }
                ],
                "IsTruncated": False,
            }

    client = Client()
    monkeypatch.setattr("boto3.client", lambda *_args, **_kwargs: client)
    store = S3ArtifactStore(
        bucket="bucket",
        endpoint_url="http://s3",
        region="region",
        access_key_id="key",
        secret_access_key="secret",
    )
    result = await store.put("jobs/id/report.md", b"stored", "text/markdown")
    assert result.size == 6
    assert client.put["Bucket"] == "bucket"
    assert await store.get(result.object_key) == b"stored"
    assert (await store.list_objects("jobs"))[0].object_key == "jobs/id/report.md"
    await store.delete(result.object_key)
    assert client.deleted["Key"] == "jobs/id/report.md"
