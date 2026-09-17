.DEFAULT_GOAL := help

.PHONY: help setup format lint test build lock run stop run-proxy stop-proxy helm-lint

help:
	@echo "setup        Install locked development dependencies"
	@echo "format       Format and auto-fix source"
	@echo "lint         Run pre-commit and mypy"
	@echo "test         Run the test suite"
	@echo "build        Build wheel and source distribution"
	@echo "lock         Refresh uv.lock"
	@echo "run          Start the local bundle"
	@echo "stop         Stop the local bundle"
	@echo "run-proxy    Start the local bundle with the OpenVPN/SOCKS overlay"
	@echo "stop-proxy   Stop the local bundle started with the proxy overlay"
	@echo "helm-lint    Lint and render the Helm chart"

setup:
	uv sync --frozen
	uv run pre-commit install

format:
	uv run ruff check --fix src tests openwebui_functions dev
	uv run ruff format src tests openwebui_functions dev

lint:
	uv run pre-commit run --all-files
	uv run mypy

test:
	uv run pytest $(PYTEST_ARGS)

build:
	uv build

lock:
	uv lock

run:
	docker compose up --build

stop:
	docker compose down

run-proxy:
	docker compose -f docker-compose.yml -f docker-compose.proxy.yaml up --build

stop-proxy:
	docker compose -f docker-compose.yml -f docker-compose.proxy.yaml down

helm-lint:
	helm lint chart/open-webui-gpt-researcher
	helm template test chart/open-webui-gpt-researcher >/dev/null
	helm template test chart/open-webui-gpt-researcher --set searxng.enabled=true >/dev/null
