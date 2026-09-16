from __future__ import annotations

import argparse
import asyncio
import os
from uuid import UUID

import uvicorn
from alembic import command
from alembic.config import Config

from .api import create_app, make_artifact_store
from .cleanup import RetentionCleaner
from .config import get_settings
from .controller import Controller
from .db import Database
from .engines import GPTResearcherEngine
from .executors import make_executor
from .function_sync import FunctionSync
from .logging import configure_logging
from .repository import JobRepository
from .runner import Runner, RunnerClient


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Open WebUI GPT Researcher service")
    subparsers = parser.add_subparsers(dest="command", required=True)
    api = subparsers.add_parser("api")
    api.add_argument("--host", default="0.0.0.0")  # noqa: S104
    api.add_argument("--port", default=8090, type=int)
    subparsers.add_parser("controller")
    subparsers.add_parser("runner")
    subparsers.add_parser("migrate")
    subparsers.add_parser("cleanup")
    sync = subparsers.add_parser("sync-functions")
    sync.add_argument("--if-configured", action="store_true")
    return parser.parse_args()


async def run_controller() -> None:
    settings = get_settings()
    if settings.mode != "k8s":
        raise SystemExit("the standalone controller is only used in k8s mode")
    database = Database(settings.database_url)
    try:
        controller = Controller(
            settings=settings,
            database=database,
            repository=JobRepository(),
            executor=make_executor(settings),
        )
        await controller.run_forever()
    finally:
        await database.close()


async def run_runner() -> None:
    settings = get_settings()
    try:
        job_id = UUID(os.environ["JOB_ID"])
        runner_token = os.environ["RUNNER_TOKEN"]
    except (KeyError, ValueError) as error:
        raise SystemExit("JOB_ID and RUNNER_TOKEN are required") from error
    client = RunnerClient(
        base_url=settings.internal_base_url,
        job_id=job_id,
        token=runner_token,
    )
    await Runner(
        settings=settings,
        client=client,
        engine=GPTResearcherEngine(
            public_search_enabled=settings.public_search_enabled,
            retriever=settings.retriever,
            scraper=settings.scraper,
        ),
    ).run()


def run_migrations() -> None:
    settings = get_settings()
    configuration = Config("alembic.ini")
    configuration.set_main_option("sqlalchemy.url", settings.database_url)
    command.upgrade(configuration, "head")


async def sync_functions(*, if_configured: bool = False) -> None:
    settings = get_settings()
    if not settings.openwebui_api_key.get_secret_value():
        if if_configured:
            return
        raise SystemExit("OPENWEBUI_API_KEY is required to sync Functions")
    sync = FunctionSync(settings)
    try:
        await sync.run()
    finally:
        await sync.close()


async def run_cleanup() -> None:
    settings = get_settings()
    database = Database(settings.database_url)
    try:
        await RetentionCleaner(
            settings=settings,
            database=database,
            repository=JobRepository(),
            artifact_store=make_artifact_store(settings),
        ).run()
    finally:
        await database.close()


def main() -> None:
    args = parse_args()
    settings = get_settings()
    configure_logging(settings.log_level)
    if args.command == "api":
        uvicorn.run(create_app(settings), host=args.host, port=args.port)
    elif args.command == "controller":
        asyncio.run(run_controller())
    elif args.command == "runner":
        asyncio.run(run_runner())
    elif args.command == "migrate":
        run_migrations()
    elif args.command == "cleanup":
        asyncio.run(run_cleanup())
    else:
        asyncio.run(sync_functions(if_configured=args.if_configured))


if __name__ == "__main__":
    main()
