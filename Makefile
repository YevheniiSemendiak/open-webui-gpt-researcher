.DEFAULT_GOAL := help

.PHONY: help setup format lint test build lock compose-up compose-down helm-lint

help:
	@echo "setup        Install locked development dependencies"
	@echo "format       Format and auto-fix source"
	@echo "lint         Run pre-commit and mypy"
	@echo "test         Run the test suite"
	@echo "build        Build wheel and source distribution"
	@echo "lock         Refresh uv.lock"
	@echo "compose-up   Start the local bundle"
	@echo "compose-down Stop the local bundle"
	@echo "helm-lint    Lint and render the Helm chart"

setup:
	uv sync --frozen
	uv run pre-commit install

format:
	uv run ruff check --fix src tests openwebui_functions
	uv run ruff format src tests openwebui_functions

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

helm-lint:
	helm lint chart/open-webui-gpt-researcher
	helm template test chart/open-webui-gpt-researcher >/dev/null
