# Installation and Open WebUI integration

This guide covers the local Docker Compose bundle, production Kubernetes installation, and the
Open WebUI configuration required for end users to run Deep Research.

## Local installation

Prerequisites are Docker Compose and enough memory for Open WebUI plus one Chromium-backed runner.

```bash
cp .env.example .env
docker compose up -d --build
```

Open <http://localhost:3000>, create the first administrator and a dedicated integration account,
then generate an API key for that account. Grant research users read access in Open WebUI to the
underlying models they should be allowed to select.

Set the integration key and suggested models in `.env`:

```dotenv
OPENWEBUI_API_KEY=...
DEFAULT_MODEL_PROFILES={"default":{"fast":"fast-model-id","smart":"report-model-id","strategic":"planning-model-id"}}
REASONING_EFFORT=low
PUBLIC_SEARCH_ENABLED=true
RETRIEVER=searx
SCRAPER=nodriver
SCRAPER_PAGE_TIMEOUT_SECONDS=120
NODRIVER_MAX_CONCURRENCY=2
SEARX_URL=http://searxng:8080
```

`SCRAPER_PAGE_TIMEOUT_SECONDS` bounds each browser-fetched source and proxy-routed Python fetcher
(including ArXiv and PDF fetches). A source that times out is omitted and a stalled Chromium
instance is recycled so the remaining research can continue. When `CRAWLER_PROXY_URL` is nonempty,
the browser and supported Python fetchers use it; leaving it empty disables the proxy adapters.
`NODRIVER_MAX_CONCURRENCY` limits active Chromium pages across all nested researchers in a runner;
lower values reduce peak memory without dropping sources, at the cost of longer scraping time.

Apply the configuration and import or update the Open WebUI Pipe and Action:

```bash
docker compose up -d --build --force-recreate api
docker compose run --rm function-sync
```

The local endpoints are:

| Component | URL |
| --- | --- |
| Open WebUI | <http://localhost:3000> |
| Research gateway API | <http://localhost:8090/docs> |
| SearXNG | <http://localhost:8081> |
| MinIO console | <http://localhost:9001> |

The default bundle performs real search, browsing, and model requests. It has no mock research
engine.

To stop it:

```bash
docker compose down
```

## Model selection and limits

`DEFAULT_MODEL_PROFILES` controls the initially suggested choices in the per-run model picker. It
does not grant access or force a model. Each user must already have Open WebUI read access to every
underlying model they select.

The gateway intersects two Open WebUI views:

- the models visible with the initiating user's authorization; and
- authoritative model metadata visible to the integration account.

Users choose separate IDs for GPT Researcher's `fast`, `smart` (report writing), and `strategic`
(planning and analysis) roles for every run. The selected IDs and limits are frozen with the job.

When Open WebUI publishes `context_length` and `max_output_tokens`, the integration uses them
automatically. If either value is missing, the model remains selectable but Open WebUI asks the
user to provide and confirm both limits for that run. The form requires:

- a context window of at least 4,096 tokens;
- a positive maximum output limit; and
- an output limit no larger than the context window.

Users must verify manually supplied values against the actual provider deployment. The researcher
does not infer missing limits and has no administrator-maintained fallback map.

For reasoning models, configure a large enough per-request maximum output for hidden reasoning
tokens. Set
`REASONING_EFFORT` when a reasoning model is exposed under a custom Open WebUI ID that GPT
Researcher cannot classify by name. Leave it blank to avoid sending a reasoning-effort override.

## Private-only research

Set `PUBLIC_SEARCH_ENABLED=false`, recreate the gateway, and rerun Function sync:

```bash
docker compose up -d --build --force-recreate api
docker compose run --rm function-sync
```

Users must then attach a file, select an Open WebUI Knowledge collection, or continue a research
chat that already contains private context.

## Optional proxy hook for local deployment

Direct egress is the default. The optional `docker-compose.proxy.yaml` overlay starts an OpenVPN
client with an internal SOCKS5 proxy and uses the gitignored
`dev/searxng/settings.local.yml` by default.

Set:

- `OPENVPN_CONFIG_DIR` to a directory containing the client configuration and referenced
  certificates;
- `OPENVPN_AUTH_FILE` to a two-line file with the username followed by the password; and
- optionally `OPENVPN_CONFIG_NAME`, which defaults to `client.ovpn`;
- optionally `VPN_BYPASS_CIDRS`, a comma- or whitespace-separated list of IPv4 CIDRs that must
  remain reachable through the original network interface.

`OPENVPN_CONFIG_DIR` and `OPENVPN_AUTH_FILE` are both required for this overlay. Start it with:

```bash
make run-proxy
```

`SEARXNG_SETTINGS_FILE` may override the host SearXNG settings file. It is mounted at
`/etc/searxng/settings.yml` in the container. Credentials and client configuration are mounted at
runtime and are not included in an image.

The bundled proxy is fail-closed: its SOCKS server sends outbound traffic only through `tun0`,
starts only after that interface exists, and exits if either OpenVPN or the tunnel disappears. A
non-privileged Kubernetes deployment therefore needs `NET_ADMIN` and access to `/dev/net/tun`;
using a privileged container only hides that device setup and grants substantially broader access.
The entrypoint automatically preserves the pre-VPN gateway and directly connected subnet. Routed
pod or service networks cannot always be inferred from a container interface—for example, Calico
commonly assigns pods a `/32`—so production deployments should supply those networks through
`VPN_BYPASS_CIDRS`.

Production deployments can set `researchJob.env.CRAWLER_PROXY_URL` to an externally managed proxy
or VPN gateway and configure SearXNG's `outgoing.proxies` to use it. The researcher chart does not
deploy network egress infrastructure.

## Kubernetes installation

Prerequisites:

- Open WebUI with `ENABLE_API_KEYS=true` and a dedicated integration account API key;
- user or group access in Open WebUI to the underlying models they may select;
- PostgreSQL and an S3-compatible bucket;
- a published image from this repository; and
- network connectivity between the research namespace and its dependencies.

No PostgreSQL extension is required for the research service schema. Use a direct connection or a
session-pooled connection for the gateway because controller election uses PostgreSQL session
advisory locks. Do not place it behind a transaction-pooling connection.

### External configuration and credentials

The chart does not create or interpret Secrets. Inject externally managed Secrets or ConfigMaps
through each component's native `envFrom` list. Secret keys must match the runtime environment
names, including:

- `DATABASE_URL`
- `SERVICE_TOKEN`
- `OPENWEBUI_API_KEY`
- `S3_ACCESS_KEY_ID`
- `S3_SECRET_ACCESS_KEY`

The API and embedded controller share `DATABASE_URL`. Bundled SearXNG normally receives
`SEARXNG_SECRET` through `searxng.envFrom`. Research Jobs do not receive provider credentials;
model and embedding requests always pass through Open WebUI.

Use `extraEnv` when a key must be renamed, or when only selected keys from a pre-existing Secret
should be exposed. `api.extraEnv`, `researchJob.extraEnv`, and `searxng.extraEnv` accept native
Kubernetes `EnvVar` objects, including `valueFrom`:

```yaml
api:
  extraEnv:
    - name: DATABASE_URL
      valueFrom:
        secretKeyRef:
          name: openwebui-postgres-pguser-openwebui
          key: uri
```

Native `postgresql://` and legacy `postgres://` values are normalized to the async PostgreSQL
driver internally. Migration, Function sync, and cleanup inherit `api.extraEnv`, just as they
inherit `api.env` and `api.envFrom`; their own `extraEnv` lists can add lifecycle-specific values.
Dynamic research Jobs receive `researchJob.extraEnv`.

Create a production values file:

```yaml
api:
  env:
    OPENWEBUI_URL: http://open-webui.open-webui.svc.cluster.local:8080
    SEARX_URL: http://research-open-webui-gpt-researcher-searxng:8080
    S3_ENDPOINT_URL: https://s3.example.com
    S3_BUCKET: research-artifacts
    ARTIFACT_PREFIX: openwebui/researcher
  envFrom:
    - secretRef:
        name: research-api

researchJob:
  env:
    CRAWLER_PROXY_URL: ""
    NODRIVER_MAX_CONCURRENCY: 2

searxng:
  enabled: true
  envFrom:
    - secretRef:
        name: research-searxng

networkPolicy:
  additionalIngress:
    - from:
        - namespaceSelector:
            matchLabels:
              kubernetes.io/metadata.name: open-webui
      ports:
        - {protocol: TCP, port: 8090}
```

Install the chart:

```bash
helm upgrade --install research ./chart/open-webui-gpt-researcher \
  --namespace research --create-namespace \
  --values values.production.yaml
```

The release performs schema migration, deploys the gateway, and runs an idempotent Function sync
Job. Every gateway replica serves the API and runs a controller task; PostgreSQL elects one active
controller. The gateway ServiceAccount has namespace-scoped permission to create and reconcile
Jobs.

Function sync publishes a namespace-qualified cluster DNS address for the gateway. When Open
WebUI runs in another namespace and the chart NetworkPolicy is enabled, allow that namespace to
reach the API port through `networkPolicy.additionalIngress`, as in the example above.

Configuration is grouped under `api`, `researchJob`, and `searxng`. Controller settings are part
of `api.env`. Migration, Function sync, and cleanup inherit the API image, environment,
`extraEnv`, `envFrom`, security contexts, scheduling, image-pull secrets, volumes, and mounts.
Their own values control lifecycle and resources.

`migration.annotations` defaults to Helm pre-install/pre-upgrade hook annotations. Override its
entries when the deployment controller needs different lifecycle annotations. Because Helm merges
maps, remove the default hook behavior by setting each `helm.sh/*` entry to `null`; the chart then
renders the migration as an ordinary Job.

Research Jobs are always available when the gateway runs and have no separate `enabled` switch.
They use a non-root, read-only security context, scoped per-job credentials, resource limits,
deadlines, and TTL cleanup.

### SearXNG

SearXNG is opt-in. With `searxng.enabled=true`, the chart deploys an internal Deployment and
Service. Set `api.env.SEARX_URL` explicitly to its Service, or to an existing installation when
`searxng.enabled=false`. The chart does not rewrite environment values.

The gateway calls SearXNG to discover URLs. Research runners use NoDriver and Chromium to retrieve
the selected pages.

## OCI chart and Argo CD

A `v*` release tag publishes:

- `ghcr.io/yevheniisemendiak/open-webui-gpt-researcher:VERSION`
- `ghcr.io/yevheniisemendiak/open-webui-gpt-researcher-openvpn-proxy:VERSION`
- `oci://ghcr.io/yevheniisemendiak/charts/open-webui-gpt-researcher:VERSION`

The exact tag version is used for the chart version, chart `appVersion`, and application image.
Because the default `api.image.tag` and `researchJob.image.tag` are empty, both components inherit
that immutable `appVersion`; operators only need to override them when intentionally deploying a
different image.

Example Argo CD source:

```yaml
source:
  repoURL: ghcr.io/yevheniisemendiak/charts
  chart: open-webui-gpt-researcher
  targetRevision: 26.9.0-a.1
  helm:
    valuesObject:
      api:
        env:
          OPENWEBUI_URL: http://open-webui.open-webui.svc.cluster.local:8080
          SEARX_URL: http://research-open-webui-gpt-researcher-searxng:8080
          S3_ENDPOINT_URL: https://s3.example.com
          S3_BUCKET: research-artifacts
          ARTIFACT_PREFIX: openwebui/researcher
        envFrom:
          - secretRef:
              name: research-api
      searxng:
        enabled: true
        envFrom:
          - secretRef:
              name: research-searxng
      networkPolicy:
        additionalIngress:
          - from:
              - namespaceSelector:
                  matchLabels:
                    kubernetes.io/metadata.name: open-webui
            ports:
              - {protocol: TCP, port: 8090}
```

Private GHCR packages require an OCI Helm repository credential in the Argo CD namespace and an
image-pull Secret in the research namespace. Public packages need neither.

Research Jobs carry an owner reference to the gateway Deployment, and runner Pods are owned by
their Jobs, so Argo CD can display Deployment → Job → Pod.

## Open WebUI Function synchronization

Function sync manages only:

- the `deep_research` Pipe;
- the Deep Research model metadata; and
- the `save_deep_research_to_knowledge` Action.

It preserves unrelated Functions, existing user Valves, model access grants, and unrelated model
actions. Re-running sync does not alter an already accepted research job. If Open WebUI API-key
route restrictions are enabled, allow the Function and model-management routes plus the
chat-completion, embedding, retrieval, chat-event, file, and Knowledge routes used by the
integration.

No public ingress is required for the research gateway. The Pipe uses the cluster-private API and,
on completion, uploads artifacts to Open WebUI with the initiating user's authorization. Open
WebUI then renders native file cards and applies ordinary ownership checks.

## Streaming and timeouts

The native Open WebUI completion remains open while research runs. Proxies in front of Open WebUI
must allow streaming responses for at least the configured wall-time. The Pipe emits an empty
structured completion chunk every `job_poll_interval_seconds`, five seconds by default, to prevent
ordinary idle timeouts.

## Research limits and retention

Users can refine logical research-query and wall-time limits through Function User Valves.
Administrator hard caps validate the values again at submission. Configure caps with:

- `HARD_MAX_QUERIES`
- `HARD_MAX_WALL_TIME_SECONDS`

`EMBEDDING_BATCH_SIZE` limits how many text chunks the gateway sends to Open WebUI in one
embeddings request. The default is `32`; lower it when the configured embedding backend has a
smaller batch limit. This does not truncate source text.

The integration does not impose aggregate input- or output-token budgets on a research run. It
uses each selected model's frozen `context_length` and `max_output_tokens` metadata to keep every
individual model request valid without prematurely reducing research quality.

Scheduled cleanup removes expired events, artifacts, jobs, and orphaned objects. Retention is
configured with `EVENT_RETENTION_DAYS`, `ARTIFACT_RETENTION_DAYS`, `JOB_RETENTION_DAYS`, and
`ORPHAN_GRACE_SECONDS`. Set the optional `ARTIFACT_PREFIX` to a relative POSIX path when multiple
deployments share a bucket. For example, `openwebui/researcher` stores objects below
`openwebui/researcher/jobs/`. Keep the prefix stable so orphan cleanup continues to scan the same
object namespace; database-referenced artifacts remain readable because their full keys are stored.

For architecture and recovery semantics, see [Architecture decisions](adrs.md).
