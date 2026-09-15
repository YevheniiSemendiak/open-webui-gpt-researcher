from __future__ import annotations

import argparse
import asyncio
import os
from uuid import UUID

import uvicorn
from alembic import command
from alembic.config import Config

from .api import create_app
from .config import get_settings
from .controller import Controller
from .db import Database
from .engines import make_engine
from .executors import make_executor
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
    return parser.parse_args()


async def run_controller() -> None:
    settings = get_settings()
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
        job_id = UUID(os.environ["RESEARCH_JOB_ID"])
        runner_token = os.environ["RESEARCH_RUNNER_TOKEN"]
    except (KeyError, ValueError) as error:
        raise SystemExit("RESEARCH_JOB_ID and RESEARCH_RUNNER_TOKEN are required") from error
    client = RunnerClient(
        base_url=settings.internal_base_url,
        job_id=job_id,
        token=runner_token,
    )
    await Runner(
        settings=settings,
        client=client,
        engine=make_engine(settings.engine, public_search_enabled=settings.public_search_enabled),
    ).run()


def run_migrations() -> None:
    settings = get_settings()
    configuration = Config("alembic.ini")
    configuration.set_main_option("sqlalchemy.url", settings.database_url)
    command.upgrade(configuration, "head")


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
    else:
        run_migrations()


if __name__ == "__main__":
    main()
