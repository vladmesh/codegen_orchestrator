# Agents and execution roles

This document describes the current LangGraph/agent ownership boundaries. It is intentionally **not**
an exhaustive inventory of every tool or node: tool names change frequently, so source modules are the
canonical inventory. Queue names and payload contracts live in [CONTRACTS.md](CONTRACTS.md).

## Product Owner (PO)

**Runtime:** the `langgraph` service.

**Source:** `services/langgraph/src/agents/po/` with transport in
`services/langgraph/src/consumers/po.py`.

The PO is a LangGraph ReactAgent that owns the user dialogue and product-level decisions. Conversation
state is persisted through the PostgreSQL checkpointer in production. Native tools are split by domain
across `tools.py`, `tools_projects.py`, `tools_stories.py`, `tools_briefs.py`,
`tools_notices.py`, and `tools_shared.py`; read those modules for the current tool inventory instead
of copying a list here.

Transport:

- inbound user/system turns: `po:input`;
- synchronous reply for one request: `po:response:{request_id}`;
- asynchronous owner notifications: `po:proactive`.

The PO does **not** directly publish application deployment work. Normal deploy admission is owned by
scheduler/lifecycle paths after the relevant PR/image evidence exists.

## Architect

**Runtime:** the `architect` container entrypoint from the shared LangGraph image.

**Source:** `services/langgraph/src/agents/architect/` and
`services/langgraph/src/consumers/architect.py`.

The Architect turns an admitted Story into dependency-aware Tasks after the project workspace is ready.
It reads the project tree/spec context and writes planning output through the API. It does not own
engineering execution or deployment.

## Engineering worker

**Runtime:** the `engineering-worker` container entrypoint.

**Source:** `services/langgraph/src/consumers/engineering.py`,
`services/langgraph/src/subgraphs/engineering.py`, and developer nodes under
`services/langgraph/src/nodes/`.

The engineering consumer accepts typed `engineering:queue` work, obtains/uses an ephemeral coding
Worker through worker-manager/broker, and persists the resulting Run/Task evidence. The coding agent
works in the story workspace and wrapper-controlled publication remains the authoritative git boundary.

## Deploy worker

**Runtime:** the `deploy-worker` container entrypoint.

**Source:** `services/langgraph/src/consumers/deploy.py` and
`services/langgraph/src/subgraphs/devops/`.

The deploy graph resolves the typed environment/secrets boundary and drives the repository's GitHub
Actions deployment workflow. Normal application deployment is **not** an Ansible deployment through
infra-service. Infra-service remains responsible for provisioning and recovery/SSH operations where
those contracts explicitly require it.

The deploy worker writes a typed deploy result. Scheduler lifecycle supervision owns the subsequent
Story transition, retry/recovery decision, QA handoff, or terminal disposition.

## QA worker

**Runtime:** the `qa-worker` container entrypoint.

**Source:** `services/langgraph/src/consumers/qa.py`, `_qa_runner.py`, related `_qa_*.py`
modules, and `services/langgraph/src/agents/qa/`.

QA runs deterministic evidence/probes first and then the assigned subscription executor. It persists a
typed QA result; scheduler supervision decides whether the Story completes or a fix Task is required.
QA does not directly own Story lifecycle transitions.

## Infra service

**Runtime:** `infra-service`; this is a service boundary, not a LangGraph agent.

It consumes provisioning work and owns Ansible/SSH infrastructure operations. Application deploys use
the deploy worker + GitHub Actions path; infra-service participates only where provisioning/recovery
contracts call for it.

## LLM channel chain

Architect, PO, and PO summarization use the ordered channel configuration from
`agent_configs.llm_channels` through `services/langgraph/src/llm/`. Channel failover is a model
transport concern and does not move business ownership between the roles above.

## Current ownership flow

```text
Telegram -> po:input -> PO -> API / architect admission
                            |
                            v
                     architect:queue
                            |
                         Architect
                            |
                         API Tasks
                            |
scheduler admission -> engineering:queue -> Engineering worker -> Run/Task evidence
                            |
story-completion + PR/CI scheduler loops -> merged PR/image evidence
                            |
                       deploy:queue
                            |
                       Deploy worker
                            |
                    typed deploy result
                            |
                 lifecycle supervision
                            |
                         qa:queue
                            |
                         QA worker
                            |
                      typed QA result
                            |
                    scheduler disposition
```

For detailed stage ordering use [PIPELINE_V2.md](PIPELINE_V2.md); for delivery/retry semantics use
[ERROR_HANDLING.md](ERROR_HANDLING.md); for queue/DTO invariants use the
[contracts index](CONTRACTS.md) plus the relevant focused guide.
