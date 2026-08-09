# Roadmap

> **Updated**: 2026-08-09 (hand-maintained)
>
> Story-level arcs only. Active work lives on the Pipeline board; the deferred pool, the plans and
> the history of past sprints live in the driving installation's knowledge store under
> `state/knowledge/projects/codegen-orchestrator/`. The internal dogfooding generators that used to
> rebuild this file were removed (`codegen_orchestrator-668`); pre-668 generated content, including
> the client-bot story dumps, is in git history.

## Current arc: stabilize core pipeline

Bring Telegram bot generation to a stable E2E state. A user comes to Telegram → talks to the PO →
gets a working bot in 20-30 minutes. Then asks for changes through dialogue → gets an
updated bot.

Stages 1-8 of the stabilization plan are complete: CI gate, template contract audit and
corrections, Sprint 002 hardening, deterministic mock smoke, template matrix, live validation and
the Telegram end-to-end. The Telegram end-to-end was verified on 2026-07-24: a message produced a
working generated bot without a manual step.

The live pipeline is verified as of **2026-08-09 on `main` 2446db88**: the full mega passed 12/12
**twice in a row** — 18:01 and 16:05, the second started a minute after the first finished, with no
cleanup between them, so the run is idempotent and not merely green once. Each run covers the noop
and LLM pipelines end to end: scaffold → engineering → CI → merge → deploy → `/health` → QA. The
run before it, also 12/12, was 2026-08-06 on the rebuilt control plane.

Infrastructure behind those runs: the control plane is `5wce` (Time4VPS 275198, since 2026-08-06),
the deploy target is `5wf9` (275301). A deploy target's SSH key lives in the database, not on the
control plane host — `deployer.py` fetches it per deploy — so the target is deliberately unreachable
from the host's own keys. Next:

- Stage 9: worker isolation hardening — mandatory before onboarding external users. Composition on
  the board: escape from the worker compose proxy to root on the host, the worker seeing the control
  plane (API and Redis on the `codegen_worker` network), and production running a bind-mounted work
  tree instead of a built image. Shipped alongside it, because external users make them measurable:
  global spend/concurrency limits with an operator kill switch, and rotation of the credentials that
  could have leaked into logs.
- Stage 10: swarm seams — on a trigger (a second worker host or sustained parallel load).

Stage 7 tail debt is tracked on the Pipeline board and gates nothing. The pre-668 numbering that
used to name it belonged to the internal tracker removed by `codegen_orchestrator-668`; those
numbers no longer resolve, so the board is the only place to look it up. Testability of private
bots is tracked separately: access is set by the template contract
(service-template 0.3.6), filling the audience from the PO menu is `codegen_orchestrator-826`, and issuing and
revoking a temporary test identity around the QA run is `codegen_orchestrator-744`.

## Next arcs

### Autonomy: smart steward

The pipeline fixes itself, and the human is the last escalation step, not the first. The prerequisite
(fail-fast and typed boundaries, phases 2-4 of sprint 002) is done.

- Fix the incident subsystem of infra-service (implement the client methods, remove the swallow wrappers)
- Failure memory: distilling run transcripts into a knowledge base for the architect and for fix tasks
- A triage agent: an escalation step before WAITING_HUMAN_REVIEW
- An active board: board events as a bus, agents subscribe, threads on the cards

### Multi-tenant hardening

Real isolation of users and projects before other people's clients appear. Overlaps with
Stage 9/10 of the stabilization plan.

- API auth + owner_id enforcement on the endpoints (backlog #1022)
- Network isolation and CPU/RAM limits for projects on shared VPS (backlog #10)
- Parallel Server Provisioning (#41)
- Per-user cost metering (LLM tokens, server resources)
- MicroVM worker runtime / elastic worker hosts — on triggers (backlog #1050, #1051)

### Product decomposition + Architect node

The PO takes a high-level description and formulates product stories; the Architect splits a story
into technical tasks with dependencies. The user sees the stories, the tasks are abstracted away. The spec:
[PIPELINE_V2.md](PIPELINE_V2.md), brainstorm bs-d302b6a1.

- Architect: story decomposition into tasks (the remainder of the arc)
- Architect: sub-story decomposition — detect that a story is too large, split it or
  return it to the PO to clarify the scope

## Later arcs (coarse-grained, the order is not fixed)

- **Frontend generation** — a frontend module in service-template; a description → a site with a domain.
- **Post-release testing** — QA through Claude Code on the prod server after a deploy: story → TESTING →
  a test based on the description, as a real user → a pass/fail loop. Brainstorm bs-eece61a8.
- **Pre-release testing** — feature environments, preview environments; E2E completion (#11),
  contract testing (schemathesis).
- **GitHub integration** — the user connects their own GitHub, sees the repository, can fork it.
  The remainder: the Repository model in the production flows (backlog #1024).
- **Admin dashboard v2/v3** — worker logs, operator intervention; then full observability
  with alerts. The remainder of the config arc: the ConfigStore TTL cache, moving the services to ConfigStore.
- **User dashboard** — a personal area for a non-technical founder: the basic version is ready (auth through
  Telegram, analytics from Loki); further development as demand appears.
- **Conversation summarization** — compressing the PO↔user conversation, context management.
- **Worker swarm** — parallel workers, container reuse (after the Stage 10 seams).
- **Security hardening** — deploy cleanup audit (#7), key encryption (#20), agent hierarchy &
  incident response (#2), rate limiting.
- **Full RAG** — search over the project/docs/conversation for the agents.

## Codegen features (deferred, as demand appears)

Generator and service-template features not tied to an arc: the scaffolder ensure-workspace gate;
eager import chains (backlog #1025); auto-routers from domain specs (#1026); make add-module
(#1027); a unified handlers error strategy; auto-updating the `__init__.py` re-exports; notifications
through a Redis Stream (#26); enum types in model fields; Celery worker support; the ddgs rename (#46);
high-level architecture spec; spec-first observability (OpenTelemetry); spec-only module storage;
standardize PYTHONPATH (backlog #1005); integration test scheduler-langgraph lifecycle (#1003).

## Deferred (after product-market fit)

- **Rust migration** — service-template and the generated services in Rust (Axum + SeaORM, Tera);
  language-agnostic YAML specs and a PoC first.
- **Human-in-the-loop** — a tariff model with escalation of tasks from the AI to live developers.

## Closed arcs

- **Dev process automation** (internal tasks/skills/doc generation) — closed by `codegen_orchestrator-668`:
  internal dogfooding was removed, orchestrator tasks go through the external pipeline. The Tasks/Stories API
  remains for client projects.
- **Admin dashboard v1** — the read-only admin panel is ready.
- **Server & application health monitoring** — node_exporter/cadvisor, health_checker, admin UI
  are ready; the remainder of drift detection is in the backlog (#1017, #1018).
- The client bots from the dogfooding era (LessWrong bot, fortune teller, cat bot, reverse bot and so on) are
  client-project stories, not orchestrator milestones; the texts are in git history.
