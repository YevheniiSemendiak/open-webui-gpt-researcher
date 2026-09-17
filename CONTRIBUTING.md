# Contributing

Use Python 3.12 and uv. Run `make setup` once, then keep `make format`, `make lint`, `make test`,
and `make helm-lint` green. Before the first production release, fold schema changes into the
existing unreleased migration; after that, add forward-only migrations. Add tests for all
authorization or state-machine changes. Do not put deployment credentials, research content, or
provider responses in fixtures.

See the [development guide](docs/development.md) for repository layout, runtime modes, optional
development overlays, Helm conventions, and documentation ownership.

Changes to the Open WebUI Function contracts should be tested against the oldest supported Open
WebUI release as well as the current pinned local-development image.
