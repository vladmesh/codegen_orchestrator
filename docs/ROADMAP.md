# Roadmap

> [!WARNING]
> Этот файл автогенерируется командой `make sync`. Не редактируйте вручную — изменения будут перезаписаны.

> **Updated**: 2026-03-08

## Phase 1: Stable Pipeline (scaffold -> code -> CI -> deploy) — COMPLETE

_Reliable pipeline from description to working deploy._

## Phase 2A: Pre-MVP (alpha blockers) — COMPLETE

_Multi-user isolation, infra prod readiness, core product flow._

## Phase 2B: Post-alpha stability

Post-alpha feedback. Stability, cleanup, optimization.

- [x] #8 Workspace Failure Counter
- [ ] #21 Deploy Pre-Check
- [ ] #7 Security Audit: Deploy Cleanup
- [ ] #10 Worker Lifecycle (Pause/Unpause)

## Phase 3: Dev Process Automation & Task Store

Dev automation + internal task management (dogfooding).

## Phase 4: Public Beta

More users. Visibility, filtering, quality.

- [ ] #2 Agent Hierarchy & Incident Response

## Phase 5: Capabilities Expansion

Frontend generation, architect node, incremental modules.

## Phase 6: Scale

Worker swarm, cost tracking, self-hosted CI.

## Backlog

- [ ] /architect skill — Story decomposition into Tasks
- [ ] Project ID → UUID + schema cleanup
- [ ] #52 Scaffold script не экранирует task_description
- [ ] #18 Split engineering_worker.py (1088 LOC)
- [ ] #54 Deploy: inter-service URL должен использовать docker service name
- [ ] #62 /brainstorm resume — продолжение обсуждения существующего драфта
- [ ] Integrate Repository into production flows (webhook, scheduler, worker)
- [ ] #59 PO work item tools (Step 4)
- [ ] #60 Engineering worker work_item lifecycle (Step 5)
- [ ] #19 Split github.py Client (986 LOC)
- [ ] #20 API Key & SSH Key Encryption
- [ ] #11 E2E Tests Completion
- [ ] #26 Notifications via Redis Stream (убрать прямую зависимость от Telegram API)
- [ ] #41 Parallel Server Provisioning
- [ ] #46 Rename duckduckgo_search → ddgs
