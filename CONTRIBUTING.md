# Contributing

Use Python 3.12 and uv. Run `make setup` once, then keep `make format`, `make lint`, `make test`,
and `make helm-lint` green. Add a migration for every persistent-schema change and tests for all
authorization or state-machine changes. Do not put deployment credentials, research content, or
provider responses in fixtures.

Changes to the Open WebUI Function contracts should be tested against the oldest supported Open
WebUI release as well as the current pinned local-development image.
