# Open WebUI GPT Researcher

Asynchronous, multi-user deep research for
[Open WebUI](https://github.com/open-webui/open-webui), powered by
[GPT Researcher](https://github.com/assafelovic/gpt-researcher).

## Accepted decisions

- Open WebUI owns users, chats, attachments, Knowledge, model access, and embeddings.
- Each run uses the initiating user's Open WebUI model catalog. The user selects the fast, smart,
  and strategic roles before approval, and the selected IDs and Open WebUI-published limits are
  frozen with the job.
- A Pipe submits research; an Action explicitly saves a completed report to Knowledge.
- GPT Researcher runs behind a durable gateway. Its MCP server is not the control plane.
- `local` mode embeds dispatch and starts real runner subprocesses; `k8s` mode uses elected
  controllers and one literal `batch/v1 Job` per research run. There is no mock engine.
- PostgreSQL stores queue/state and provides leader election. S3-compatible storage holds
  production artifacts. Valkey and a project-specific vector database are not required.
- SearXNG discovers public URLs and headless NoDriver/Chromium extracts selected pages. Public
  search may be disabled when the job has an attachment or Knowledge source.
- Public searches pass through the gateway for atomic per-job accounting. Untrusted snippets and
  pages are bounded before they enter model prompts.
- The gateway reserves a conservative input-token estimate before a model call, reconciles it with
  provider-reported usage, and terminates impossible jobs with the actual budget reason.
- Only the gateway holds the Open WebUI integration account API key. A runner receives a random,
  scoped, per-job credential.
- Function sync is targeted and idempotent. It does not replace unrelated Functions or interrupt
  an already accepted run.
- Reports are saved to Knowledge only when the user explicitly invokes the Action.
- A chat is a research thread: later requests inherit its prior messages, files, Knowledge, and
  reports. A new chat starts a new thread; natively referenced chats become read-only context.
- Continuation reports deduplicate source identities and always expose a **Changes since previous
  iteration** section.
- Runtime Jobs are owned by the controller Deployment and runner Pods are owned by their Job, so
  Argo CD displays Deployment → Job → Pod.
- GPT Researcher is pinned to an exact source revision because the current PyPI `0.16.0` artifact
  predates its importability fix.

## Architecture

```mermaid
flowchart LR
  U["Open WebUI user"] --> F["Deep Research Pipe"]
  F --> A["Gateway API"]
  A --> P[("PostgreSQL")]
  A --> S["Artifact store"]
  A --> O["Open WebUI APIs"]
  C["Elected controller"] --> K["Research Job"]
  K --> R["Runner"]
  R --> A
  R --> G["GPT Researcher"]
  G --> Q["SearXNG"]
  Q --> W["Public web"]
  G -->|"headless NoDriver"| W
  G -->|"private sub-query"| A
  A -->|"frozen source IDs"| O
```

The source manifest is frozen on submission. GPT Researcher's generated private sub-queries go
through Open WebUI retrieval, so attachments and Knowledge participate throughout the run without
coupling this service to Open WebUI's selected vector-store implementation.

The Pipe keeps Open WebUI's normal streamed completion open while the durable job runs, emitting
application-level keepalives and UI status events. The final report therefore passes through Open
WebUI's own completion persistence rather than relying on a public researcher URL or on an
out-of-band rewrite of another user's message. The job itself remains durable if the browser
disconnects; its event and artifact records are not tied to the browser connection.

## Local installation

Prerequisites are Docker Compose and enough memory for Open WebUI plus one Chromium-backed runner.

```bash
cp .env.example .env
docker compose up -d --build
```

Open <http://localhost:3000>, create the first administrator and a dedicated integration account,
then generate an API key for that account. Grant research users read access in Open WebUI to the
underlying models they should be allowed to select. Put the key and suggested model IDs into `.env`:

```dotenv
OPENWEBUI_API_KEY=...
DEFAULT_MODEL_PROFILES={"default":{"fast":"fast-model-id","smart":"report-model-id","strategic":"planning-model-id"}}
REASONING_EFFORT=low
PUBLIC_SEARCH_ENABLED=true
RETRIEVER=searx
SCRAPER=nodriver
SEARX_URL=http://searxng:8080
```

The default profile only orders the suggested choices in the per-run model picker. It does not
grant access or force a model. The gateway intersects the model IDs returned with the initiating
user's authorization with metadata returned by Open WebUI to the integration account. This is
necessary because Open WebUI can omit provider limits from the user-scoped response; it preserves
the user's access boundary while keeping Open WebUI authoritative. The user chooses separate model
IDs for GPT Researcher's `fast`, `smart` (report writing), and `strategic` (planning/analysis) roles
for every run.

When Open WebUI publishes `context_length` and `max_output_tokens`, those authoritative values are
used automatically. A model with incomplete metadata remains selectable, but selecting it opens a
required per-run form for the user to provide and confirm both limits. The pipe accepts whole token
counts, requires a context window of at least 4,096 tokens, and rejects an output limit larger than
the context window. Invalid values are prompted again and no job is submitted without valid limits.
Users should verify manually entered limits against the actual provider deployment; the values are
not inferred by the researcher.

Both Open WebUI-provided and user-provided limits are frozen in the submitted job. This makes a
running job reproducible even if Open WebUI metadata changes later. There is intentionally no
administrator-side fallback map or duplicate model-window configuration in the researcher.
For reasoning models, allow enough output budget for hidden reasoning tokens;
models that spend their entire completion allowance on reasoning are unsuitable
for report writing unless their output limit and reasoning settings are tuned.
`REASONING_EFFORT` is optional; set it when a reasoning model is exposed under a
custom OpenWebUI ID that GPT Researcher cannot classify by name.

Apply the configuration and import/update the two Open WebUI Functions:

```bash
docker compose up -d --build --force-recreate api
docker compose run --rm function-sync
```

Open WebUI is on <http://localhost:3000>, the gateway API on
<http://localhost:8090/docs>, SearXNG on <http://localhost:8081>, and the MinIO console on
<http://localhost:9001>. The default stack performs real search, browsing, and model requests.

To run private-only research, set `PUBLIC_SEARCH_ENABLED=false`, recreate `api`, rerun
`function-sync`, and select at least one attachment or Knowledge source.

### Optional Firecrawl experiment

Firecrawl is retained only as a future comparison/integration overlay; it is not part of the
default stack or production recommendation. It introduces its own PostgreSQL, Redis, Playwright,
API, and worker processes. Run it explicitly with:

```bash
docker compose -f docker-compose.yml -f docker-compose.firecrawl.yaml up -d --build
```

The overlay deliberately has no RabbitMQ or extract worker. Revalidate its topology against the
chosen Firecrawl revision before considering production use.

After upgrading a checkout that previously ran Firecrawl by default, stop its old containers with
`docker compose down --remove-orphans` before starting the new default stack. Named data volumes are
retained unless `--volumes` is added.

## Kubernetes installation

Prerequisites:

- Open WebUI with `ENABLE_API_KEYS=true` and a dedicated integration account API key;
- Open WebUI read access for each research user or group to the underlying models they may select;
- PostgreSQL, an S3-compatible bucket, and cluster-internal SearXNG with JSON results enabled;
- a published image from this repository; and
- allowed network paths between the research namespace and those services.

Provide a Secret through the deployment environment's secret-management mechanism:

```yaml
apiVersion: v1
kind: Secret
metadata:
  name: research-secrets
  namespace: research
type: Opaque
stringData:
  database-url: "postgresql+asyncpg://..."
  service-token: "..."
  signing-secret: "..."
  openwebui-api-key: "..."
  s3-access-key-id: "..."
  s3-secret-access-key: "..."
```

Install the chart:

```bash
helm upgrade --install research ./chart/open-webui-gpt-researcher \
  --namespace research --create-namespace \
  --set image.repository=ghcr.io/OWNER/open-webui-gpt-researcher \
  --set image.tag=0.1.0 \
  --set env.OPENWEBUI_URL='http://open-webui.open-webui.svc.cluster.local:8080' \
  --set env.SEARX_URL='http://searxng.searxng.svc.cluster.local:8080' \
  --set env.S3_ENDPOINT_URL='https://s3.example.com' \
  --set env.S3_BUCKET='research-artifacts' \
  --set secrets.existingSecret=research-secrets
```

The chart runs schema migration, deploys API/controllers, and runs an idempotent Function sync Job.
PostgreSQL session advisory locks elect one controller; use a direct or session-pooled database
connection, not a transaction-pooling proxy. Runner Jobs use a non-root, read-only security context,
resource limits, deadline, scoped token, and TTL. The image contains Chromium for NoDriver.

SearXNG itself is intentionally outside this chart. Keep its Service cluster-internal and set
`env.SEARX_URL`. The gateway, not runner Pods, calls SearXNG. No PostgreSQL extension is required
for this project's schema.

### OCI chart and Argo CD

A `v*` release tag publishes:

- `ghcr.io/OWNER/open-webui-gpt-researcher:VERSION`;
- `oci://ghcr.io/OWNER/charts/open-webui-gpt-researcher:VERSION`.

Example Argo CD source:

```yaml
source:
  repoURL: ghcr.io/OWNER/charts
  chart: open-webui-gpt-researcher
  targetRevision: 0.1.0
  helm:
    valuesObject:
      image:
        repository: ghcr.io/OWNER/open-webui-gpt-researcher
        tag: 0.1.0
      env:
        OPENWEBUI_URL: http://open-webui.open-webui.svc.cluster.local:8080
        SEARX_URL: http://searxng.searxng.svc.cluster.local:8080
        S3_ENDPOINT_URL: https://s3.example.com
        S3_BUCKET: research-artifacts
      secrets:
        existingSecret: research-secrets
```

For private GHCR packages, configure an OCI Helm repository credential in the Argo CD namespace
and a separate image-pull Secret in the research namespace. Public packages need neither.

## Open WebUI integration

Function sync manages only `deep_research`, its model metadata, and the
`save_deep_research_to_knowledge` action using Open WebUI APIs. It associates the action only with
the Deep Research model. Existing user Valves, unrelated Functions, and unrelated model actions are
preserved. If API-key route restrictions are enabled, allow Function and model management plus the
chat-completion, embedding, retrieval, chat-event, file, and Knowledge routes used by the
integration.

The research gateway remains cluster-internal. Completed artifacts stay in the configured artifact
store until the user selects **Attach research files**. The action downloads them over the private
network and uploads them to Open WebUI with the current user's authorization, so Open WebUI renders
native file cards and enforces normal ownership. **Save report to Knowledge** is a separate explicit
action and creates or updates Knowledge as the current user. No public researcher ingress is
required for either operation. `env.ARTIFACT_BASE_URL` is retained only for direct API clients that
explicitly want signed artifact URLs; chat rendering does not use those URLs.

### Iterative and linked-chat research

- Submit another Deep Research request in the same chat to create the next numbered iteration.
  The latest successful job is recorded as its parent.
- Start a new chat to start an independent research thread.
- Use Open WebUI's **More → Reference Chats** picker to link another chat. The Pipe reads only
  references the current user can access and snapshots their active message branches.
- Current-chat and directly linked-chat messages, attachments, and Knowledge selections are
  included. Linked chats are not followed recursively, preventing accidental context expansion.
- Conversation context is deterministically bounded by the Pipe's `context_char_limit` Valve.
- Reports and source records are deduplicated per run. Saving any report to Knowledge remains an
  explicit user action.

Because the native completion stays open, proxies in front of Open WebUI must permit streaming
responses for at least the configured research wall-time. The Pipe emits an empty structured
completion chunk every `job_poll_interval_seconds` (five seconds by default) to keep ordinary idle
timeouts from firing.

## Budgets and recovery

Users refine input-token, output-token, search, and wall-time budgets through Function Valves;
administrator hard caps validate those values at submission. Input estimation is intentionally
model-agnostic and conservative because Open WebUI may route many tokenizer families. Actual
accounting uses the provider's reported usage.

State and ordered events are durable in PostgreSQL. Expiring dispatch leases recover controller
crashes. Scheduled cleanup removes expired events, artifacts, jobs, and orphaned objects. Runtime
settings use direct uppercase names such as `DATABASE_URL`, `DEFAULT_MODEL_PROFILES`, `SEARX_URL`,
`JOB_ID`, and `RUNNER_TOKEN`. The Helm chart exposes these non-secret settings directly under
`env:`; credentials remain Kubernetes Secret references.

## Development

```bash
make setup
make format
make lint
make test
make build
make helm-lint
```

Tests fake external boundaries only; production has no mock research engine.

## Decision evidence

- [Open WebUI Function types](https://docs.openwebui.com/features/extensibility/plugin/functions/)
- [Open WebUI API keys](https://docs.openwebui.com/features/authentication-access/api-keys/)
- [Open WebUI Function API implementation](https://github.com/open-webui/open-webui/blob/main/backend/open_webui/routers/functions.py)
- [GPT Researcher configuration](https://docs.gptr.dev/docs/gpt-researcher/gptr/config)
- [GPT Researcher import fix in the pinned revision](https://github.com/assafelovic/gpt-researcher/commit/bea0ad07c3ead12517c79e03789da49a270fd249)
- [SearXNG search API](https://docs.searxng.org/dev/search_api.html)
- [NoDriver implementation used by pinned GPT Researcher](https://github.com/assafelovic/gpt-researcher/blob/master/gpt_researcher/scraper/browser/nodriver_scraper.py)
- [Helm OCI registries](https://helm.sh/docs/topics/registries/)
- [Argo CD Helm/OCI repositories](https://argo-cd.readthedocs.io/en/stable/operator-manual/declarative-setup/#helm)
- [PostgreSQL advisory locks](https://www.postgresql.org/docs/current/explicit-locking.html#ADVISORY-LOCKS)
- [Argo CD resource tracking](https://argo-cd.readthedocs.io/en/stable/user-guide/resource_tracking/)

## License

MIT. GPT Researcher and Open WebUI retain their respective licenses.
