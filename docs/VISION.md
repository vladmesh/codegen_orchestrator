# Product Vision

## What this is

A platform for automatic project generation and deployment. The user describes what they want in Telegram and gets a deployed project with CI/CD, a domain and SSL in 20-30 minutes. Then they iterate through dialogue: "add authorization", "make the bot send pictures" — the system understands the context and extends the project.

The interface is a Telegram bot. The PO agent runs a Socratic dialogue, clarifies the requirements and formulates the spec. After that the pipeline does everything itself: scaffold → architect → code → CI → deploy → QA.

## Who it is for

Non-technical founders and entrepreneurs who need a working MVP in minutes, not weeks. No need to know programming, no need to hire a developer, no need to figure out deployment.

## Project types

Right now — Telegram bots in Python (python-telegram-bot/aiogram + a FastAPI backend + PostgreSQL). This is the main and the only stable type.

I want to support:
- **Web applications** — SPA/SSR with a backend and a database. The same flow: a description → a ready site with a domain. A large epic.
- **Rust services** — Axum + SeaORM as an alternative to Python. A stricter feedback loop for the agents, faster builds. Start with a PoC, then full support.
- **Modular assembly** — predefined modules (auth, payments, notifications) that can be added to an existing project through `make add-module`.

## Iteration through dialogue

The key feature is not one-off generation but **continuous extension**. The user says "I want my weather bot to also send pictures of cats" → the system figures out which project it is, adds the functionality, tests it and deploys it. Without recreating the project.

The PO has to be able to:
- Pick an existing project of the user to extend
- Understand the context of previous extensions
- Tell a "new project" from an "extension of an existing one"

## Scale and multi-tenancy

Right now — a single operator (me). The target scale is 15-20 simultaneous users without conflicts and lags.

What is needed for that:
- Row-level data isolation (RLS or app-level scoping)
- Project-scoped secrets with encryption
- Rate limiting on external API calls per tenant
- Worker pool management: a limit on parallel workers, a task queue under overload
- Load monitoring and alerts

## Observability

For the operator (me):
- **Admin panel** — a single dashboard: all users, projects, workers, token spend, service health
- **Logs** — centralized logs of all microservices (Loki + Grafana), search and filtering
- **LLM tracing** — full visibility of LLM calls: prompts, responses, tool usage, latency, cost
- **Queue health** — monitoring of the Redis streams, stuck messages, consumer lag

For the user:
- **Dashboard** — product metrics for their projects: DAU/WAU, requests/day, p95, error rate
- **Project status** — "how are my projects doing?" → a health and load report through the PO

## Health of deployed projects

After a deploy the project is not abandoned, the system watches it:
- HTTP health probing + an SSL expiry check
- Container drift detection (orphans/ghosts)
- Automatic recovery on a crash (restart/redeploy)
- A notification to the user if something went down
- Post-release QA: Claude Code on the prod servers tests it like a real user

## Human-in-the-loop

Workers can run into a blocker (unclear requirements, a lack of resources, a bug in the infrastructure). Instead of failing silently there is an escalation:
- The worker signals "blocked" with a description of the problem
- The admin sees the blocker, can give guidance and resume
- The PO can reopen a completed story if the problem repeats

In the future — a tariff model: a basic subscription (AI only) → an expensive one (live developers join through the orchestrator).

## Monetization (ideas)

- **Managed API integrations** — the user does not bring their own OpenAI/SendGrid key but spends platform credits
- **Credit-based billing** — internal credits, payment per API call through the platform gateway
- **Cost tracking** — accounting of token spend per user/project to form the prices

## What we do NOT do

- Not a general-purpose CI/CD platform
- Not a code review tool
- Not a self-hosted solution (for now)
- Not multi-language beyond Python + Rust
- Not a low-code builder — we generate real code, the user can fork it and develop it themselves

---

## Architectural invariants

The audit checks every one of these points. A violation = a bug.

1. Services communicate ONLY through Redis Streams or API calls. No cross-service imports.
2. All statuses are enums in shared/contracts/. No hardcoded strings.
3. All queue messages are Pydantic DTOs in shared/contracts/queues/. No raw dicts.
4. Fail-fast everywhere. No .get(key, default), no fallback values, no silent None handling.
5. Worker = an ephemeral Docker container with a CLI agent. Nothing else is called a "worker".
6. Secrets never reach the LLM context. Handles in state, Python resolves the values.
7. Every service owns its own models. shared/ contains only contracts (DTOs, enums, queue schemas).
8. Logging only through structlog. No print(). All events are structured with correlation IDs.
