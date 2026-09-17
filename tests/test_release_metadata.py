from __future__ import annotations

from pathlib import Path

import pytest
from dev.resolve_release_metadata import resolve_release_metadata

ROOT = Path(__file__).resolve().parents[1]


def test_prerelease_tag_is_the_authoritative_version() -> None:
    metadata = resolve_release_metadata(
        tag="v26.9.0-a.1",
        owner="YevheniiSemendiak",
        repository="YevheniiSemendiak/Open-WebUI-GPT-Researcher",
    )

    assert metadata.version == "26.9.0-a.1"
    assert metadata.major_minor == "26.9"
    assert metadata.stable is False
    assert metadata.image == "ghcr.io/yevheniisemendiak/open-webui-gpt-researcher"
    assert metadata.chart_ref == (
        "oci://ghcr.io/yevheniisemendiak/charts/open-webui-gpt-researcher:26.9.0-a.1"
    )


def test_stable_release_enables_floating_tags() -> None:
    metadata = resolve_release_metadata(
        tag="v26.9.0",
        owner="Owner",
        repository="Owner/Repository",
    )

    assert metadata.version == "26.9.0"
    assert metadata.stable is True


@pytest.mark.parametrize(
    "tag",
    [
        "26.9.0",
        "v26.09.0",
        "v26.9",
        "v26.9.0-alpha.1",
        "v26.9.0+build.1",
        "vlatest",
    ],
)
def test_invalid_or_non_portable_release_tags_are_rejected(tag: str) -> None:
    with pytest.raises(ValueError, match="release tag must match"):
        resolve_release_metadata(tag=tag, owner="owner", repository="owner/repository")


def test_publish_workflow_does_not_use_checked_in_versions() -> None:
    workflow = (ROOT / ".github/workflows/publish.yml").read_text()

    assert "workflow_dispatch" not in workflow
    assert "chart_version=" not in workflow
    assert "VERSION=${{ needs.metadata.outputs.version }}" in workflow
    assert "PACKAGE_VERSION" not in workflow
    assert "RELEASE_VERSION" not in workflow
    assert '--version "$CHART_VERSION"' in workflow
    assert '--app-version "$CHART_VERSION"' in workflow
