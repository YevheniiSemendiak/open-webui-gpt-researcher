# Open WebUI GPT Researcher

Asynchronous, multi-user deep research for
[Open WebUI](https://github.com/open-webui/open-webui), powered by
[GPT Researcher](https://github.com/assafelovic/gpt-researcher).

## Accepted decisions

- Open WebUI owns users, chats, attachments, Knowledge, models, and embeddings.
- A Pipe submits research; an Action explicitly saves completed reports to Knowledge.
- GPT Researcher runs behind a durable gateway. Its MCP server is not the control plane.
- There are two deployment modes: `local` and `k8s`. There is no mock research engine.
- Local mode embeds dispatch in the API container and starts real runner subprocesses.
- Kubernetes mode uses elected controller replicas and one `batch/v1 Job` per run.
- PostgreSQL stores queue/state and provides controller election. Valkey is not required.
- S3-compatible storage holds production artifacts. GPT Researcher never accesses the vector
  database directly; private retrieval goes through Open WebUI.
- Public search is optional. With it disabled, a run must select an attachment or Knowledge.
- The gateway alone holds the Open WebUI integration account's admin API key. Runners receive only
  a random per-job credential.
- Function installation is an idempotent, targeted Open WebUI API sync. It does not delete or
  replace unrelated Functions.
- Runtime Jobs are owned by the controller Deployment; runner Pods are owned by their Job. Argo CD
  therefore shows Deployment → Job → Pod in the application resource tree.
- The gateway stays cluster-internal unless direct artifact downloads are enabled.

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
  G -. "optional" .-> W["Public web"]
  G -->|"private sub-query"| A
  A -->|"frozen source IDs"| O
```

The source manifest is frozen when a user approves the plan. GPT Researcher's generated
sub-queries use the gateway-backed Open WebUI retriever, so attachments and Knowledge participate
throughout research.

## Deployment modes

### Local

`RESEARCH_MODE=local` runs API, queue dispatcher, and runner subprocesses in one researcher
container. PostgreSQL, MinIO, and Open WebUI remain separate Compose services. This executes the
real GPT Researcher package and is intended for development and integration tests.

Runner subprocesses share the API container's resources and terminate if it restarts. No Docker
socket is mounted and no sibling runner containers are created.

### Kubernetes

`config.mode=k8s` runs a separate two-replica controller Deployment. PostgreSQL session advisory
locks elect one dispatcher. The leader checks the database backend PID before dispatch; connection
loss releases leadership. Use a direct PostgreSQL connection or session-pooling proxy, not a
transaction-pooling proxy.

Each run is an isolated Kubernetes Job with resource limits, deadline, scoped credential, and TTL.
The Job owns its Pod and is owned by the stable controller Deployment. A normal controller rollout
preserves the Deployment UID; deleting/recreating it garbage-collects active Jobs.

## Kubernetes installation

Prerequisites:

- Open WebUI with `ENABLE_API_KEYS=true`;
- a dedicated admin/integration account and its API key;
- PostgreSQL and an S3-compatible bucket;
- a published image from this repository; and
- network access from the research namespace to Open WebUI, PostgreSQL, object storage, and the
  chosen model/search providers.

Provide a Secret with these keys:

```yaml
apiVersion: v1
kind: Secret
metadata:
  name: research-secrets
  namespace: research
type: Opaque
stringData:
  service-token: "..."
  signing-secret: "..."
  openwebui-api-key: "..."
  s3-access-key-id: "..."
  s3-secret-access-key: "..."
```

Install:

```bash
helm upgrade --install research ./chart/open-webui-gpt-researcher \
  --namespace research --create-namespace \
  --set image.repository=ghcr.io/OWNER/open-webui-gpt-researcher \
  --set image.tag=0.1.0 \
  --set config.databaseUrl='postgresql+asyncpg://...' \
  --set config.openwebuiUrl='http://open-webui.open-webui.svc.cluster.local:8080' \
  --set config.s3EndpointUrl='https://s3.example.com' \
  --set config.s3Bucket='research-artifacts' \
  --set secrets.existingSecret=research-secrets
```

### Published chart and Argo CD

The release workflow publishes both artifacts on a `v*` tag:

- image: `ghcr.io/OWNER/open-webui-gpt-researcher:VERSION`;
- Helm chart: `oci://ghcr.io/OWNER/charts/open-webui-gpt-researcher:VERSION`.

The tag without its `v` prefix, the chart `version`, and `appVersion` must match. For example,
`v0.1.0` publishes version `0.1.0`. A manual workflow run publishes the versions currently in
`Chart.yaml`.

An Argo CD Application can consume the OCI chart directly. In Helm repository syntax, `repoURL`
does not include the `oci://` prefix:

```yaml
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: open-webui-gpt-researcher
  namespace: argocd
spec:
  project: default
  source:
    repoURL: ghcr.io/OWNER/charts
    chart: open-webui-gpt-researcher
    targetRevision: 0.1.0
    helm:
      valuesObject:
        image:
          repository: ghcr.io/OWNER/open-webui-gpt-researcher
          tag: 0.1.0
        imagePullSecrets:
          - name: ghcr-pull
        config:
          databaseUrl: postgresql+asyncpg://research:password@postgresql:5432/research
          openwebuiUrl: http://open-webui.open-webui.svc.cluster.local:8080
          s3EndpointUrl: https://s3.example.com
          s3Bucket: research-artifacts
        secrets:
          existingSecret: research-secrets
  destination:
    server: https://kubernetes.default.svc
    namespace: research
  syncPolicy:
    automated:
      prune: true
      selfHeal: true
    syncOptions:
      - CreateNamespace=true
```

GHCR packages are private on first publication. Either make both packages public, or configure two
independent credentials:

1. Give Argo CD access to the chart with a repository Secret in the Argo CD namespace. Set
   `type: helm`, `enableOCI: "true"`, `url: ghcr.io/OWNER/charts`, and inject a username plus a token
   with `read:packages` as `password`.
2. Create `ghcr-pull` as a `kubernetes.io/dockerconfigjson` Secret in the `research` namespace so
   Kubernetes can pull the private application and runner image. Argo CD's repository credential is
   not an image pull credential.

Manage both Secrets through the deployment environment's secret-management mechanism; do not
commit their values. Public GHCR packages support anonymous pulls, so neither credential is needed
when both artifacts are public, and `imagePullSecrets` can be omitted.

The chart runs its schema migration, deploys API/controllers, and then runs a post-install or
post-upgrade Function sync Job. The sync uses these targeted endpoints:

- `GET /api/v1/functions/export`;
- `POST /api/v1/functions/create` or `/id/{id}/update`;
- `POST /api/v1/functions/id/{id}/valves/update`; and
- `POST /api/v1/functions/id/{id}/toggle` when activation is required.

It manages only `deep_research` and `save_deep_research_to_knowledge`. Re-importing code does not
interrupt an accepted job: the Pipe is no longer involved after submission. Existing Open WebUI
user Valves and unrelated Functions are preserved. Future Pipe/Action invocations load the new
code. Keep Function changes backward-compatible with existing report markers and Valve data.

If API-key endpoint restrictions are enabled, allow the Function endpoints above plus the gateway's
chat-completion, embedding, retrieval, chat-event, file, and Knowledge routes.

The chart's NetworkPolicy permits research-namespace callers. Add the Open WebUI and ingress
controller namespace/pod selectors through `networkPolicy.additionalIngress` when they run in
other namespaces.

Search/provider credentials used only by runners belong in the Secret named by
`config.runner.extraEnvSecret`; never put the Open WebUI API key there.

## Local installation

```bash
cp .env.example .env
docker compose up -d --build
```

Open <http://localhost:3000>, create the first administrator and a dedicated integration account,
then generate its API key. Configure `.env`:

```dotenv
RESEARCH_OPENWEBUI_API_KEY=...
RESEARCH_MODEL_PROFILES={"default":"model-id-visible-in-openwebui"}
RESEARCH_PUBLIC_SEARCH_ENABLED=true
RETRIEVER=tavily
TAVILY_API_KEY=...
```

Apply the credentials and import/update the Functions:

```bash
docker compose up -d --build --force-recreate api
docker compose run --rm function-sync
```

The Pipe and Action are activated automatically. Open WebUI is at <http://localhost:3000>, the
gateway API at <http://localhost:8090/docs>, and MinIO Console at <http://localhost:9001>.

For private-only research:

```dotenv
RESEARCH_PUBLIC_SEARCH_ENABLED=false
TAVILY_API_KEY=
```

Recreate `api`, rerun `function-sync`, and select an attachment or Knowledge when submitting.
Tavily is only GPT Researcher's default public retriever; another supported retriever can be
selected with `RETRIEVER`.

## Downloads and Knowledge

`config.artifactBaseUrl` is only the browser-facing base for signed artifact links. Leave it empty
and keep ingress disabled for chat reports without direct downloads. To offer downloads, expose the
artifact route through a user-reachable private or public proxy and set the base URL.

Reports are never added to Knowledge automatically. The user invokes Save Deep Research to
Knowledge, then chooses a new collection or an existing writable collection.

## Budgets and recovery

Administrator Function defaults and service hard caps bound input tokens, output tokens, searches,
and wall time. Users can refine per-run values; the gateway validates them. With the default Open
WebUI model route, the gateway accounts provider-reported tokens and clamps subsequent calls.

Job state and ordered events are durable in PostgreSQL. API replicas are stateless. Controller
leadership survives pod loss, but the narrow crash window between committing `dispatched` and
creating the Kubernetes Job still needs reconciliation; see [docs/roadmap.md](docs/roadmap.md).

Runtime Jobs deliberately do not copy Argo CD's tracking annotation: they are dependent live
resources, not Git-desired manifests.

## Development

```bash
make setup
make format
make lint
make test
make build
make helm-lint
```

Tests replace external boundaries with fakes; production contains only the GPT Researcher engine.

## Decision evidence

- [Open WebUI Function types](https://docs.openwebui.com/features/extensibility/plugin/functions/)
- [Open WebUI API keys](https://docs.openwebui.com/features/authentication-access/api-keys/)
- [Open WebUI Function API implementation](https://github.com/open-webui/open-webui/blob/main/backend/open_webui/routers/functions.py)
- [GPT Researcher configuration](https://docs.gptr.dev/docs/gpt-researcher/gptr/config)
- [Helm OCI registries](https://helm.sh/docs/topics/registries/)
- [Argo CD declarative Helm/OCI repositories](https://argo-cd.readthedocs.io/en/stable/operator-manual/declarative-setup/#helm)
- [GitHub Container Registry authentication](https://docs.github.com/en/packages/working-with-a-github-packages-registry/working-with-the-container-registry)
- [PostgreSQL advisory locks](https://www.postgresql.org/docs/current/explicit-locking.html#ADVISORY-LOCKS)
- [Argo CD resource tracking](https://argo-cd.readthedocs.io/en/stable/user-guide/resource_tracking/)
- [Argo CD Resources view](https://argo-cd.readthedocs.io/en/stable/user-guide/resources-view/)

## License

MIT. GPT Researcher and Open WebUI retain their respective licenses.
