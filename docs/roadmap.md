# Roadmap

## Before the first production rollout

- Pin and qualify an exact Open WebUI and GPT Researcher compatibility matrix.
- Add a dispatch lease/reconciler for the crash window between committing `dispatched` and creating
  the Kubernetes Job.
- Add integration tests against real Open WebUI, PostgreSQL, MinIO, and a Kubernetes test cluster.
- Define retention jobs for database events, generated artifacts, and orphaned object keys.
- Add metrics, traces, audit events, and dashboards for queue age, runtime, failures, tokens, and
  retriever activity.
- Perform prompt-injection and cross-user authorization testing with realistic Knowledge data.

## UX and capability follow-ups

- Editable research plans and follow-up questions before approval.
- Native citation/source events and richer progress phase details in Open WebUI.
- UI-assisted Knowledge collection selection instead of the initial `id:<knowledge-id>` input.
- Policy-scoped MCP server catalogs, per-job tool grants, and tool-call audit trails.
- Optional sandboxed code execution for analysis tasks that require computation or file conversion.
- PDF/DOCX report rendering as an independent artifact worker.
