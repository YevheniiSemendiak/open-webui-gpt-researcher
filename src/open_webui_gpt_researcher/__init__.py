"""Open WebUI GPT Researcher integration service."""

from importlib.metadata import PackageNotFoundError, version
from os import getenv


def _release_version() -> str:
    if configured := getenv("VERSION"):
        return configured
    try:
        return version("open-webui-gpt-researcher")
    except PackageNotFoundError:
        return "0.0.0-dev"


__version__ = _release_version()
