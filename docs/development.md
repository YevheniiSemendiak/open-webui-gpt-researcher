# Development guide

This document contains contributor workflows, internal implementation notes, and optional
development-only experiments. User-facing behavior belongs in the root README, installation and
operator procedures belong in `installation.md`, and architecture decisions belong in `adrs.md`.

## Setup

The project uses Python 3.12, `uv`, Ruff, MyPy, pytest, pre-commit, Docker Compose, and Helm.

```bash
make setup
```

The standard checks are:

```bash
make format
make lint
make test
make build
make helm-lint
```

CI keeps the component checks in the reusable `.github/workflows/tests.yaml` workflow. Its final
`Check` job waits for every required job and fails unless all of them succeeded. The main
`.github/workflows/ci.yml` workflow calls it for pushes and pull requests; after it succeeds,
Dependabot pull requests are approved and configured for squash auto-merge.

Repository settings must allow GitHub Actions to approve pull requests and auto-merge must be
enabled for the Dependabot automation to complete.

Tests fake external boundaries only. Production code does not include a mock research engine.

## Repository layout

| Path | Purpose |
| --- | --- |
| `src/open_webui_gpt_researcher/` | Gateway, controller, runner, persistence, and integrations |
| `openwebui_functions/` | Pipe and Knowledge Action synchronized into Open WebUI |
| `migrations/` | Alembic PostgreSQL migrations |
| `chart/open-webui-gpt-researcher/` | Helm chart and JSON values schema |
| `tests/` | Unit and integration-style tests with external boundaries replaced |
| `dev/` | Local service configuration and helper containers |
| `.github/workflows/` | CI and release workflows |

## Runtime modes

`MODE=local` runs the gateway and embedded controller in one process and starts a real runner
subprocess per accepted job. This is the Docker Compose and test-development topology.

`MODE=k8s` runs the same gateway/controller process, but dispatches one literal Kubernetes
`batch/v1 Job` per research run. PostgreSQL advisory locks elect one active controller task across
all gateway replicas.

Do not introduce a third mock runtime mode. Tests should replace external boundaries while keeping
the production orchestration paths intact.

## Local bundle

Start the complete local stack with:

```bash
make run
```

Stop it with:

```bash
make stop
```

After changing `.env`, recreate the affected service. Function source or managed Valve changes
also require rerunning the idempotent sync Job:

```bash
docker compose up -d --build --force-recreate api
docker compose run --rm function-sync
```

## Optional proxy overlay

`docker-compose.proxy.yaml` exists to test search and crawling through an externally routed
network path. It adds the OpenVPN/SOCKS helper and replaces the SearXNG settings file with the
gitignored `dev/searxng/settings.local.yml` unless `SEARXNG_SETTINGS_FILE` is set.

Use `make run-proxy` and `make stop-proxy`. Credential injection and operator-facing setup are
documented in [Installation](installation.md#optional-proxy-hook-for-local-deployment).
The proxy discovers its original gateway and directly connected subnet before OpenVPN starts.
Use `VPN_BYPASS_CIDRS` for routed client networks that are not visible on the container interface.

Keep the proxy optional. Production Helm templates expose proxy connection hooks but must not
manage a VPN Deployment or its credentials.

## Optional Firecrawl experiment

Firecrawl is retained only for future comparison and is not the production recommendation. Its
overlay introduces PostgreSQL, Redis, Playwright, API, and worker processes:

```bash
docker compose -f docker-compose.yml -f docker-compose.firecrawl.yaml up -d --build
```

The overlay deliberately has no RabbitMQ or extract worker. Revalidate its topology against the
selected Firecrawl revision before relying on it.

If an older checkout ran Firecrawl by default, remove its obsolete containers without deleting
named data volumes:

```bash
docker compose down --remove-orphans
```

## Database and migrations

PostgreSQL is the durable queue, event store, job store, and controller-election backend. The
research schema does not require PostgreSQL extensions. Migrations run through Alembic and are
packaged into the application image.

During early development, update the existing unreleased migration rather than creating migration
chains for schemas that have never reached production. Once a released deployment exists, append
forward-only migrations instead.

## Open WebUI Function development

Function sync is targeted and idempotent. It may update managed source and administrator Valves,
but must preserve:

- unrelated Functions and Actions;
- user-specific Valves;
- existing Deep Research model access grants;
- unrelated actions associated with the Deep Research model; and
- already accepted or running research jobs.

Exercise end-user behavior through the Open WebUI interface after changing Pipe streaming,
attachments, Actions, or interactive forms. API-only assertions are insufficient for UI lifecycle
changes.

## Helm development

Values are grouped by runtime component. Keep deployment-owned settings under `api`, dynamically
created Job settings under `researchJob`, and bundled search settings under `searxng`.

The chart deliberately does not manage external secrets, ingress, PodDisruptionBudgets, or an
external proxy/VPN. Use native `envFrom`, scheduling, affinity, labels, annotations, volumes, and
mounts from the relevant component block.

Validate chart changes with:

```bash
make helm-lint
helm template test chart/open-webui-gpt-researcher --set searxng.enabled=true
```

When Kubernetes schemas are available, also validate rendered resources with `kubeconform`.

## Dependency and image changes

- Keep Python dependencies locked in `uv.lock`.
- Preserve Docker layer caching by installing locked dependencies before copying frequently
  changing application source.
- GPT Researcher is pinned to an exact source revision; review upstream changes before advancing
  it.

GPT Researcher's native cost estimator remains enabled. Its `tiktoken` dependency may download an
encoding vocabulary on first use, so production egress must permit that request or the required
vocabulary must be present in the image/cache before a restricted-egress Job starts. Open WebUI
provider usage remains authoritative for this integration's enforced token accounting.

## Releases

The Git tag is the sole release-version authority. Do not edit package metadata, the Helm chart,
or image tags before a release. Their checked-in `0.0.0.dev0` and `0.0.0-dev` values are deliberate
development placeholders.

Push a tag on the commit to release:

```bash
git tag -s v26.9.0-a.1
git push origin v26.9.0-a.1
```

Accepted tags are `vMAJOR.MINOR.PATCH` and the prerelease forms `vMAJOR.MINOR.PATCH-a.N`,
`-b.N`, and `-rc.N`. The workflow validates the tag once and derives all published metadata from
it:

- the application and optional OpenVPN proxy images receive the exact version tag;
- the OCI chart receives the exact version as both chart version and `appVersion`;
- the Python distribution metadata receives the equivalent PEP 440 version;
- the API and synchronized Open WebUI Functions expose the exact release version.

Stable releases additionally update the `MAJOR.MINOR` and `latest` image tags. Prereleases never
update floating tags. The tagged commit must already contain the publishing workflow, so fix a
failed workflow on a new commit and tag that commit; do not expect an existing tag to use workflow
changes made afterward.

## Documentation ownership

- `README.md`: product overview, use cases, user workflow, short examples, and documentation links.
- `docs/installation.md`: local and Kubernetes installation, integration, configuration, and
  operational procedures.
- `docs/development.md`: contributor setup, internal implementation guidance, debugging, and
  experimental tooling.
- `docs/adrs.md`: accepted architecture decisions, their rationale and consequences, and evidence.

Avoid copying the same instructions into multiple documents. Link to the owning document instead.
