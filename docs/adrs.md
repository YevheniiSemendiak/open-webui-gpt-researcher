# Architecture decision records

This document records accepted decisions for the Open WebUI GPT Researcher integration. Decisions
are current unless explicitly marked superseded.

## ADR-001: Open WebUI remains the user-facing system of record

**Status:** Accepted

Open WebUI owns users, authorization, chats, attachments, Knowledge collections, model access, and
embeddings. The researcher does not introduce parallel user, ACL, chat, Knowledge, or vector-store
models.

Private sub-queries return through Open WebUI retrieval using the initiating user's authorization.
This keeps the integration independent of the vector database selected by an Open WebUI operator.

**Consequences:**

- Model and source access follows Open WebUI permissions.
- The researcher stores frozen source references and run artifacts, not a reusable competing
  Knowledge implementation.
- Saving a report to Knowledge remains an explicit user action.

## ADR-002: Use a Pipe for research and an Action for Knowledge persistence

**Status:** Accepted

The Deep Research Pipe participates in the native chat completion lifecycle, submits durable work,
streams status and keepalives, and attaches completed files. A separate Action saves a completed
report to Knowledge only when the user requests it.

Automatically creating Knowledge entries after every run was rejected because it would create
clutter and retain potentially sensitive research without an explicit decision.

**Consequences:**

- Normal completion automatically attaches all artifacts to the final message.
- **Attach research files** is only an idempotent recovery action for interrupted historical
  transfers.
- **Save report to Knowledge** remains explicit and executes with the current user's authorization.

## ADR-003: Place GPT Researcher behind a durable gateway

**Status:** Accepted

GPT Researcher's MCP server is not the control plane. A project-owned gateway performs admission,
authorization propagation, operational limit enforcement, durable state, event collection,
cancellation, retries, artifact storage, and Open WebUI callbacks.

**Consequences:**

- GPT Researcher remains replaceable behind a stable job contract.
- Browser disconnects do not cancel accepted work.
- Long-running jobs can be reconciled independently of an Open WebUI request process.

## ADR-004: Embed the controller in the gateway process

**Status:** Accepted

Every gateway replica serves the HTTP API and runs a controller task in the same process.
PostgreSQL session advisory locks elect one active controller across the replicas.

A separate controller Deployment was rejected because API and controller use the same code,
database, release lifecycle, and low operational scale. Separating them added image, environment,
ServiceAccount, scheduling, and rollout configuration without a current scaling requirement.

**Consequences:**

- API and controller share scaling and failure boundaries.
- The gateway ServiceAccount has namespace-scoped Job-management permissions.
- Multiple gateway replicas remain safe, but their database connection must support session locks;
  transaction pooling is unsuitable.

## ADR-005: Use equivalent local and Kubernetes execution paths

**Status:** Accepted

`local` mode starts a real runner subprocess per job. `k8s` mode creates one literal Kubernetes
`batch/v1 Job` per research run. There is no production mock engine or mock runtime mode.

Kubernetes Jobs receive owner references to the gateway Deployment, while runner Pods remain owned
by their Job. This produces a visible Deployment → Job → Pod hierarchy in Argo CD.

**Consequences:**

- Docker Compose can exercise complete research without a Kubernetes cluster.
- Production runs receive independent resource limits, deadlines, security contexts, scheduling,
  retries, logs, and cleanup.
- Tests replace external boundaries rather than using a divergent implementation.

## ADR-006: Use PostgreSQL for durability and election, S3 for production artifacts

**Status:** Accepted

PostgreSQL stores jobs, ordered events, leases, cumulative usage telemetry, and cleanup metadata.
It also provides controller leader election through advisory locks. Production artifacts use an
S3-compatible store.

Valkey is not required for the durable queue, and the researcher does not require its own vector
database. Open WebUI remains responsible for embeddings and Knowledge retrieval.

**Consequences:**

- The research schema requires no PostgreSQL extensions.
- Local filesystem artifacts remain suitable for development, while production can use any
  compatible S3 provider.
- Operators do not need a project-specific Redis/Valkey deployment.

## ADR-007: Separate URL discovery from page extraction

**Status:** Accepted

SearXNG discovers candidate public URLs. NoDriver and Chromium retrieve selected pages inside the
runner. Public search can be disabled when a run includes attachments, Knowledge, or existing chat
context.

Public search requests pass through the gateway so per-job query accounting is atomic. Private
retrieval fan-out uses the same gateway and is recorded separately as telemetry. Untrusted snippets
and page contents are bounded before entering model prompts.

Firecrawl is retained only as an optional development experiment because its supporting services
add significant operational weight. It is not the production default.

**Consequences:**

- SearXNG does not crawl the result pages on behalf of the runner.
- Search and crawler egress can independently use an externally managed proxy or VPN gateway.
- The project provides connection hooks but does not deploy production VPN infrastructure.

## ADR-008: Treat Open WebUI as authoritative for model access and metadata

**Status:** Accepted

Each run uses the initiating user's Open WebUI model catalog. The Pipe intersects user-visible model
IDs with authoritative metadata retrieved through the integration account. Users choose separate
fast, smart, and strategic models before approval.

When Open WebUI does not publish `context_length` and `max_output_tokens`, selecting that model
requires the user to provide both values. Unknown values are a configuration error, not a reason to
silently apply a guessed default.

The selected IDs and limits are frozen with the job. The researcher has no
`MODEL_CONTEXT_WINDOWS` or `DEFAULT_MODEL_CONTEXT_WINDOW` fallback configuration.
All model and embedding requests pass through Open WebUI. A direct provider route was rejected
because it would create a second authorization and model-configuration authority.

**Consequences:**

- Research cannot grant access to a model the user cannot normally use.
- Metadata changes do not alter an already accepted job.
- Custom provider aliases remain usable after explicit per-run confirmation of missing limits.

## ADR-009: Separate research shape from operational limits

**Status:** Accepted

Users choose a focused, balanced, broad, deep, or custom research tree. Breadth controls initial
parallel research directions, depth controls recursive follow-up levels, and queries per branch
maps to GPT Researcher's per-worker planning iterations.

`max_queries` is a hard ceiling on the logical research tree, not a request to generate additional
work. Query and wall-time limits have administrator-controlled caps. Public search calls consume
the runtime query allowance; private retriever fan-out is recorded separately and does not
multiply the user-visible query count.

Aggregate input- and output-token budgets are intentionally not imposed because they can truncate
source processing or terminate a useful research run based on cumulative usage rather than model
capability. The gateway still constrains every request to the selected model's frozen context and
maximum-output limits. Provider-reported token usage remains recorded as telemetry.

**Consequences:**

- The Pipe rejects a research shape that cannot fit its query limit.
- Retrying a failed runner cannot reset or bypass the accepted query and wall-time limits.
- A model call fails clearly when its input cannot fit the selected model's context window.

## ADR-010: Use scoped runner credentials

**Status:** Accepted

Only the gateway holds the Open WebUI integration account API key. Each runner receives a random,
job-scoped credential used only for its internal job endpoints.

**Consequences:**

- Runner environments do not contain the Open WebUI integration key.
- A credential from one run cannot access another run.
- The gateway validates the runner credential and active job state before accepting artifact writes.
- Runner credentials are revoked when a job reaches a terminal state.
- Research Jobs can be scheduled and isolated independently of the gateway.

## ADR-011: Make Function synchronization targeted and idempotent

**Status:** Accepted

Function sync manages only the Deep Research Pipe, its model metadata, and the Knowledge Action. It
preserves unrelated Functions, user-specific Valves, model access grants, and unrelated model
actions.

**Consequences:**

- Argo CD or Helm synchronization can safely rerun Function sync.
- Reimporting managed Functions does not modify the immutable specification of an accepted job.
- An ongoing research run is not interrupted by a release synchronization.

## ADR-012: Model a chat as an iterative research thread

**Status:** Accepted

A later Deep Research request in the same chat continues the research thread. A new chat starts a
new thread. Chats selected through Open WebUI's native reference mechanism are added as read-only
context when the user can access them.

Current and directly referenced chat messages, files, Knowledge selections, and successful reports
are snapshotted within deterministic context bounds. References are not followed recursively.

Reports and sources are deduplicated for each run, and continuation reports include a **Changes
since previous iteration** section.

## ADR-013: Deliver artifacts through Open WebUI

**Status:** Accepted

The research gateway remains cluster-internal. On successful completion, the Pipe downloads all
artifacts over the private service path and uploads them to Open WebUI using the initiating user's
authorization. The final response emits native Open WebUI file attachments.

Public researcher download URLs were rejected as the normal chat path because they require another
public ingress and bypass Open WebUI's ordinary ownership experience.

**Consequences:**

- No public gateway ingress is required for chat usage.
- Open WebUI controls file visibility and download authorization.
- Artifact content is available only through the authenticated cluster-private API used by the
  Open WebUI Pipe and Action.

## ADR-014: Report progress through stable integration events

**Status:** Accepted

The runner converts GPT Researcher activity into stable lifecycle events: starting, context
preparation, planning, researching, writing, and finalization. It also reports bounded aggregates
such as batch progress, latest query, successful page reads, connected-tool result counts, and
distinct visited URLs.

Raw logs, partial report text, and unstable vendor telemetry are not exposed directly to users.
Events are filtered, aggregated, and deduplicated before persistence.

## ADR-015: Preserve the requested report language

**Status:** Accepted

The final report must use the language explicitly requested by the user. If no report language is
specified, it uses the language of the latest research request. Integration-generated continuation
instructions do not change that decision.

The integration augments the upstream report instruction instead of maintaining a divergent fork
of GPT Researcher's prompt set.

## ADR-016: Pin GPT Researcher to an exact source revision

**Status:** Accepted

The selected GPT Researcher source revision contains an importability fix that is newer than the
current PyPI `0.16.0` artifact. Git tags and PyPI versions are therefore not interchangeable for
this integration.

**Consequences:**

- Builds are reproducible against the reviewed commit.
- Advancing GPT Researcher requires explicit compatibility and end-to-end testing.

## ADR-017: Derive release versions exclusively from Git tags

**Status:** Accepted

A release tag is the sole authority for the application image tag, OCI chart version, chart
`appVersion`, Python package metadata, API version, and synchronized Open WebUI Function versions.
Checked-in metadata uses development placeholders and is never manually prepared for a release.

Release tags use `vMAJOR.MINOR.PATCH`, optionally followed by `-a.N`, `-b.N`, or `-rc.N`. Exact
version tags are immutable. Only stable releases update the floating `MAJOR.MINOR` and `latest`
image tags.

**Consequences:**

- A version mismatch between checked-in files can no longer block an otherwise valid tagged
  release.
- The packaged chart selects the same exact-version application image by default.
- A publishing-workflow fix requires a tag on a commit containing that fix; changing the branch
  does not alter the workflow associated with an existing tag.

## ADR-018: Size reports from evidence coverage

**Status:** Accepted

Research breadth, depth, and queries per branch control evidence collection. Report length is not
a user or infrastructure limit. The integration supplements the upstream prompt with an
evidence-driven stopping rule: cover every requested aspect supported by the gathered evidence,
avoid repetition and padding, and then conclude. The adapter removes the upstream minimum-word
instruction so its built-in default does not become an implicit report-size control.

## ADR-019: Preserve streaming across the model gateway

**Status:** Accepted

When GPT Researcher requests a streaming chat completion, the research gateway passes Open
WebUI's server-sent events through without buffering the complete response. This is particularly
important for final report generation, where GPT Researcher submits the aggregated research
context in one request and generation may take several minutes.

The gateway requests usage in the final stream event and accounts it after the stream closes. If
the provider omits usage, the gateway conservatively estimates input and output tokens from the
request and streamed content. Non-streaming callers retain the existing JSON response path.

## Recovery and retention consequences

Dispatch leases recover a gateway failure before a runner starts. Active runners persist
heartbeats. When a heartbeat expires, the controller stops any remaining executor, retries the job
from scratch up to `RUNNER_MAX_ATTEMPTS`, and eventually marks it failed explicitly.

`RUNNER_HEARTBEAT_SECONDS` defaults to 10 and `RUNNER_STALE_SECONDS` defaults to 60. Scheduled
cleanup removes expired events, artifacts, jobs, and orphaned objects according to the configured
retention periods.

## Evidence

- [Open WebUI Function types](https://docs.openwebui.com/features/extensibility/plugin/functions/)
- [Open WebUI API keys](https://docs.openwebui.com/features/authentication-access/api-keys/)
- [Open WebUI Function API implementation](https://github.com/open-webui/open-webui/blob/main/backend/open_webui/routers/functions.py)
- [GPT Researcher configuration](https://docs.gptr.dev/docs/gpt-researcher/gptr/config)
- [GPT Researcher import fix in the pinned revision](https://github.com/assafelovic/gpt-researcher/commit/bea0ad07c3ead12517c79e03789da49a270fd249)
- [SearXNG search API](https://docs.searxng.org/dev/search_api.html)
- [SearXNG outbound proxy settings](https://docs.searxng.org/admin/settings/settings_outgoing.html)
- [NoDriver implementation used by GPT Researcher](https://github.com/assafelovic/gpt-researcher/blob/master/gpt_researcher/scraper/browser/nodriver_scraper.py)
- [Helm OCI registries](https://helm.sh/docs/topics/registries/)
- [Argo CD Helm and OCI repositories](https://argo-cd.readthedocs.io/en/stable/operator-manual/declarative-setup/#helm)
- [PostgreSQL advisory locks](https://www.postgresql.org/docs/current/explicit-locking.html#ADVISORY-LOCKS)
- [Argo CD resource tracking](https://argo-cd.readthedocs.io/en/stable/user-guide/resource_tracking/)
