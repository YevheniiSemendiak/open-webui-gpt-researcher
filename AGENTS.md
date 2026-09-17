# Repository agent instructions

## Documentation ownership

Keep documentation organized by audience and do not turn the root README into an implementation
manual.

- `README.md` owns the brief product overview, end-user use cases, user workflow, short usage
  examples, high-level architecture, and links to detailed documentation.
- `docs/installation.md` owns local and Kubernetes installation, Open WebUI integration,
  configuration, credentials, upgrades, and operator procedures.
- `docs/development.md` owns contributor setup, codebase internals, development tweaks, debugging,
  test workflows, and experimental tooling.
- `docs/adrs.md` owns accepted architecture decisions, rationale, tradeoffs, consequences, and
  supporting evidence.
- `docs/roadmap.md` owns planned or explicitly deferred work.

When behavior changes, update the owning document and link to it from other documents instead of
duplicating instructions. Move superseded decisions into a clearly marked superseded ADR rather
than silently deleting their history once the project has shipped them.

## Project constraints

- Open WebUI remains authoritative for identity, authorization, chats, model access, attachments,
  Knowledge, and embeddings.
- Keep `local` and `k8s` execution paths behaviorally aligned; do not add a production mock mode.
- Do not add chart-managed external secrets, ingress, PodDisruptionBudgets, or VPN infrastructure
  without an explicit architecture decision.
- Preserve unrelated Open WebUI Functions, Actions, model associations, and user Valves during
  synchronization.
- Validate user-visible lifecycle changes from the Open WebUI end-user perspective, not only by
  calling internal APIs.

## Validation

Run checks proportionate to the change. The complete validation set is:

```bash
make format
make lint
make test
make build
make helm-lint
docker compose config --quiet
```

Also render optional Compose overlays or Helm components when changing them.
