# Deep Research in Open WebUI: User Guide

Deep Research runs long-form investigations across public web sources, attached files, Knowledge
collections, and chat context. When the run finishes, the report and supporting files appear in
the originating chat automatically.

## Quick start

1. Create a new chat or open a chat containing an earlier research run.
2. Select the **Deep Research** model.
3. If needed, attach files, select Knowledge collections, or reference other chats.
4. Describe what should be investigated, which aspects matter, the intended scope, and the report
   language.
5. Send the message.
6. Select models for the three research roles.
7. Review the proposed plan and approve the run.
8. Return to the chat when the report is ready. You may close the page while research continues.

> **Screenshot placeholder 1:** Selecting **Deep Research** in the chat model picker.

## Demo request

The following request demonstrates public-source research, comparison, citations, structured
reporting, and practical recommendations. The `balanced` strategy is a good starting point; use
`broad` if you want wider coverage.

```text
Research the current landscape of self-hosted AI chat and knowledge platforms for organizations.
Compare Open WebUI, LibreChat, AnythingLLM, Dify, and Onyx.

For each platform, evaluate:
- multi-user operation, authentication, SSO, roles, and access controls;
- supported model providers and model gateways;
- file ingestion, RAG, shared knowledge, and vector-store flexibility;
- tools, agents, MCP or comparable extensibility mechanisms;
- Kubernetes deployment, scaling, background jobs, and operational complexity;
- observability, auditability, security considerations, licensing, and project activity.

Prefer official documentation, source repositories, release notes, and other primary sources.
Clearly distinguish verified facts from interpretation. Cite every important claim with a direct
source link and mention when comparable information is unavailable.

Finish with:
1. a concise executive summary;
2. a comparison matrix;
3. major strengths, limitations, and operational risks of each option;
4. recommendations for an organization with 100–500 users that operates Kubernetes and needs
   private knowledge collections;
5. a short list of questions that should be validated in a proof of concept.

Write the report in English.
```

> **Screenshot placeholder 2:** The demo request entered in the chat composer.

### Demo request: LLM evolution in theoretical mathematics and physics

Use the `deep` strategy for this interdisciplinary request.

```text
Research how the evolution of large language models and related AI systems is influencing
theoretical mathematics and theoretical physics.

Cover developments from the emergence of transformer-based language models to the present. Focus
on changes in research practice, discovery, verification, collaboration, and the kinds of problems
researchers can realistically attempt.

Investigate the following areas:

1. Theoretical mathematics
- automated and interactive theorem proving;
- formalization with Lean, Isabelle, Coq, and comparable systems;
- generation of conjectures, proofs, counterexamples, and useful intermediate lemmas;
- mathematical search, symbolic reasoning, and integration with computer algebra;
- notable results involving systems such as AlphaGeometry, FunSearch, AlphaProof, AlphaEvolve,
  and LLM-based theorem provers;
- whether these systems have produced genuinely novel mathematics or mainly accelerated existing
  workflows.

2. Theoretical physics
- symbolic derivations and manipulation of complex equations;
- quantum field theory, string theory, general relativity, cosmology, and condensed-matter theory;
- identifying symmetries, conservation laws, dualities, and candidate physical models;
- simulation design, surrogate models, and interpretation of numerical results;
- connections between LLMs, scientific foundation models, differentiable programming, and
  automated scientific discovery;
- examples where AI contributed to a new theoretical result rather than merely summarizing
  literature.

3. Evolution of capabilities
- how the transition from text prediction to reasoning models, tool use, code execution,
  retrieval, multimodal input, and formal verification changed scientific usefulness;
- which improvements arise from larger models and which depend on external tools, search, formal
  systems, or specialized training;
- current limitations in long-horizon reasoning, mathematical rigor, physical intuition,
  uncertainty estimation, and reproducibility.

4. Impact on scientific work
- how mathematicians and physicists currently use these systems;
- effects on research speed, division of labor, collaboration, education, and peer review;
- whether LLMs are changing which researchers or institutions can participate in advanced
  theoretical work;
- risks of plausible but invalid proofs, fabricated references, benchmark contamination,
  automation bias, and concentration of research infrastructure.

5. Future outlook
- plausible developments over the next 3–5 years;
- requirements for trustworthy AI-assisted mathematical and physical discovery;
- the likely roles of formal verification, autonomous research agents, specialized scientific
  models, and human–AI collaboration;
- which parts of theoretical research are most and least likely to be transformed.

Use peer-reviewed papers, official project publications, primary research artifacts, benchmark
results, and statements from practicing mathematicians and physicists. Prefer original sources
over news coverage.

For every prominent claim:
- provide a direct citation;
- distinguish demonstrated results from laboratory prototypes and speculation;
- explain whether the evidence measures correctness, novelty, usefulness, or only benchmark
  performance;
- identify significant criticism, failed replication, or contrary evidence;
- avoid treating performance on mathematical benchmarks as proof of general scientific reasoning.

Conclude with:
1. an executive summary;
2. a timeline of major capability changes and scientific milestones;
3. separate assessments for mathematics and theoretical physics;
4. a table of representative systems, their methods, achievements, evidence, and limitations;
5. an analysis of what LLMs can do independently versus what requires formal tools or human
   guidance;
6. three scenarios for the next five years: conservative, probable, and transformative;
7. a final verdict on whether LLMs are currently tools for acceleration, genuine partners in
   discovery, or an early form of autonomous scientific reasoning.

Write the report in English for a technically literate audience. Explain specialized terminology
without oversimplifying the mathematics or physics.
```

## Writing an effective request

A useful request normally identifies:

- the topic and primary question;
- the aspects that must be investigated;
- the desired level of fact checking;
- the relevant time period or geography;
- the expected report structure;
- the report language.

Example:

```text
Research the Ukrainian market for knowledge-management systems for medium-sized companies as of
2026. Compare the main vendors, features, deployment models, risks, and approximate costs.
Separate verified facts from assumptions and link to primary sources. Write the report in English.
```

If no language is specified, the report uses the language of the research request.

## Sources and context

Before submitting the request, you can add:

- **files** through the attachment control in the message composer;
- **Knowledge collections** that you are allowed to access;
- **other chats** through **More → Reference Chats**.

Deep Research also uses previous messages, files, and successful reports from the current chat.
Access to attached or referenced material follows ordinary Open WebUI permissions.

> **Screenshot placeholder 3:** Attaching a file and selecting a Knowledge collection.

> **Screenshot placeholder 4:** Referencing another chat through **Reference Chats**.

## Research strategy

You can change the strategy in the **Deep Research** user settings (`Valves`) before starting a
run.

| Strategy | Recommended use | Estimated maximum queries |
| --- | --- | ---: |
| `focused` | A narrow question, quick verification, or a few specific facts | 5 |
| `balanced` | A typical investigation with useful coverage and detail | 25 |
| `broad` | Many parallel topics, market participants, or alternatives | 49 |
| `deep` | Successive investigation of related and dependent questions | 71 |
| `custom` | A user-defined research shape | Calculated before submission |

`balanced` is the recommended initial choice for most requests.

> **Screenshot placeholder 5:** The `Valves` dialog showing the strategy and default values.

### Custom strategy

The `custom` strategy exposes these controls:

- **Breadth** (`1–8`) — the number of initial parallel research directions. Increase it to cover
  more independent aspects of the topic.
- **Depth** (`1–4`) — the number of recursive follow-up levels. Increase it to investigate
  dependencies, causes, and related questions.
- **Queries per branch** (`1–5`) — the number of search queries generated within each research
  direction.
- **Max queries** — a hard limit on logical search queries. It cannot exceed the administrator's
  infrastructure limit.
- **Max wall time seconds** — the maximum duration of the run in seconds.

The resolved research shape is shown before the run starts. If it does not fit within
`Max queries`, increase that value or reduce breadth, depth, or queries per branch.

## Model selection

After the request is submitted, Deep Research asks you to select accessible models for three
roles:

- **Fast model** — quick intermediate work, search-query generation, and processing individual
  sources;
- **Smart model** — analysis and final report writing;
- **Strategic model** — planning research directions and making complex search decisions.

Administrator-suggested models appear first, but you may choose any model you are permitted to
use. If Open WebUI does not provide a selected model's context and output limits, Deep Research
asks you to enter them for that run.

> **Screenshot placeholder 6:** Selecting Fast, Smart, and Strategic models.

## Reviewing the plan

Before starting, Deep Research displays a summary containing:

- the research question;
- the available context;
- the selected strategy and its parameters;
- query and time limits;
- the selected models.

For requests longer than 1,000 characters, the dialog shows the first and last 500 characters
separated by `...`. The complete request is still submitted unchanged.

Approve the dialog if the plan is correct. Otherwise, reject it, adjust the request or `Valves`,
and submit the message again.

> **Screenshot placeholder 7:** The research-plan confirmation dialog.

## Following progress

The chat displays accurate stages such as context preparation, planning, searching, reading,
analysis, and report writing. You do not need to keep the page open for a long-running job. The
result is added to the originating chat after completion.

> **Screenshot placeholder 8:** Progress updates during an active research run.

## Report and artifacts

After successful completion, the chat contains the rendered report and attached files:

- `report.md` — the final report;
- `research-notes.md` — research notes;
- `sources.json` — a structured source list;
- `run.json` — information about the completed run.

The files are stored in Open WebUI, so they can be viewed or downloaded without direct access to
the Deep Research service.

> **Screenshot placeholder 9:** A completed report with its attached files.

## Continuing an investigation

Submit the next request in the **same chat** to extend or revise the result. For example:

```text
Re-evaluate the conclusions using the newly attached document. Identify contradictions with the
previous report, update the recommendations, and list the changes since the previous iteration in
a separate section.
```

The next iteration includes the earlier conversation, reports, files, and Knowledge selected in
that chat. A new chat starts an independent research thread. To reuse context from another thread,
add it through **Reference Chats**.

## Saving a report to Knowledge

Saving to Knowledge is an explicit user action:

1. Run **Save report to Knowledge** on the completed message.
2. Enter a name to create a new private collection.
3. To append the report to an existing collection, enter `id:<knowledge-id>`.
4. Open **Workspace → Knowledge** to verify the result.

> **Screenshot placeholder 10:** The **Save report to Knowledge** action on a completed message.

> **Screenshot placeholder 11:** Entering a new collection name or `id:<knowledge-id>`.

## Practical tips

- Start with `balanced`; increase the research shape only when the task requires it.
- Put fact-checking criteria and the expected report structure directly in the request.
- For sensitive internal work, review the files, Knowledge, and chats included as context.
- State the desired language explicitly when it matters.
- Continue in the same chat to refine or expand existing findings.
- Use a new chat when you want an independent investigation without prior-thread context.
