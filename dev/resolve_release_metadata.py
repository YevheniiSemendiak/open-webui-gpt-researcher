from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

RELEASE_TAG = re.compile(
    r"^v(?P<major>0|[1-9][0-9]*)\."
    r"(?P<minor>0|[1-9][0-9]*)\."
    r"(?P<patch>0|[1-9][0-9]*)"
    r"(?:-(?P<prerelease>a|b|rc)\.(?P<prerelease_number>0|[1-9][0-9]*))?$"
)


@dataclass(frozen=True)
class ReleaseMetadata:
    version: str
    major_minor: str
    stable: bool
    image: str
    chart_registry: str
    chart_ref: str


def resolve_release_metadata(*, tag: str, owner: str, repository: str) -> ReleaseMetadata:
    match = RELEASE_TAG.fullmatch(tag)
    if match is None:
        raise ValueError(
            "release tag must match vMAJOR.MINOR.PATCH, optionally followed by -a.N, -b.N, or -rc.N"
        )

    version = tag.removeprefix("v")
    normalized_owner = owner.lower()
    normalized_repository = repository.lower()
    chart_registry = f"oci://ghcr.io/{normalized_owner}/charts"
    return ReleaseMetadata(
        version=version,
        major_minor=f"{match.group('major')}.{match.group('minor')}",
        stable=match.group("prerelease") is None,
        image=f"ghcr.io/{normalized_repository}",
        chart_registry=chart_registry,
        chart_ref=f"{chart_registry}/open-webui-gpt-researcher:{version}",
    )


def main() -> None:
    if os.environ.get("GITHUB_REF_TYPE") != "tag":
        raise SystemExit("release publication requires a Git tag")
    try:
        metadata = resolve_release_metadata(
            tag=os.environ["GITHUB_REF_NAME"],
            owner=os.environ["GITHUB_REPOSITORY_OWNER"],
            repository=os.environ["GITHUB_REPOSITORY"],
        )
    except (KeyError, ValueError) as error:
        raise SystemExit(str(error)) from error

    output_path = os.environ.get("GITHUB_OUTPUT")
    if not output_path:
        raise SystemExit("GITHUB_OUTPUT is required")
    outputs = {
        "chart_registry": metadata.chart_registry,
        "chart_ref": metadata.chart_ref,
        "image": metadata.image,
        "major_minor": metadata.major_minor,
        "stable": str(metadata.stable).lower(),
        "version": metadata.version,
    }
    with Path(output_path).open("a", encoding="utf-8") as output:
        for name, value in outputs.items():
            output.write(f"{name}={value}\n")


if __name__ == "__main__":
    main()
