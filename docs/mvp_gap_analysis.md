# ORCHESTRATOR MVP GAP ANALYSIS

## Executive Summary
The Codegen Orchestrator has a solid "Happy Path" vertical slice. The logic for interacting with users, creating GitHub repos, and syncing them is in place. However, it completely lacks the **Resilience**, **Security**, and **Operations** layers required for a "Real" production project.

**Current State:** prototype / proof-of-concept.
**Target State:** Stable MVP for daily use.

## Critical Gaps (Must Fix for MVP)

### 1. 🚨 Data Loss on Restart (Resilience)
- **Problem**: `services/langgraph/src/graph.py` uses `MemorySaver` for checkpointing.
- **Impact**: Every time the `langgraph` container redeploys (which is necessary for code updates), **ALL** active conversation threads and process states are lost. Users are stranded.
- **Fix**: Replace `MemorySaver` with `PostgresSaver` (using `langgraph-checkpoint-postgres`).

### 2. 🔓 Zero Access Control (Security)
- **Problem**: `services/telegram_bot` accepts messages from **ANY** Telegram user. There is no whitelist of allowed `user_id`s.
- **Impact**: If the bot username leaks, anyone can trigger resource allocation, see project details, or theoretically spawn costly workers.
- **Fix**: Implement an `ALLOWED_USER_IDS` environment variable and middleware in the bot to reject unauthorized users immediately.

### 3. 🏁 Scheduler Race Conditions (Reliability)
- **Problem**: `services/scheduler` runs `while True` loops for syncing. If you scale the scheduler to >1 replica (for high availability), or if a rolling update overlaps, multiple instances will run simultaneously.
- **Impact**: Duplicate database inserts, racing GitHub API calls, potential data corruption.
- **Fix**: Implement Distributed Locking (using Redis) for all background tasks. e.g. `with redis_lock("github_sync", timeout=300): ...`.

### 4. 📝 Missing Deployment Engine (Feature)
- **Problem**: The "DevOps" node is largely a placeholder. The system can "create" a repo, but it cannot "deploy" the resulting application to a server.
- **Impact**: You can't actually *run* the projects you build without manual intervention.
- **Fix**: Implement the `Ansible` wrapper in `services/infrastructure` and wire it into the `devops` node.

### 5. 📉 No Production Observability (Ops)
- **Problem**: `docs/LOGGING.md` describes a beautiful JSON logging setup, but the infrastructure to **collect and view** it (Prometheus, Loki, Grafana) is missing from `docker-compose.yml`.
- **Impact**: Debugging production issues requires grepping raw `docker logs`. No alerts on failures.
- **Fix**: Add `loki`, `promtail`, `grafana` to the stack. Configure `structlog` to ship to Loki (or use Promtail to scrape Docker stdout).

## Recommended Roadmap

### Phase 1: Stability (The "Don't Lose Data" Update)
1.  **Persist State**: Integrate `langgraph-checkpoint-postgres`.
2.  **Lock Scheduler**: Add Redis Distributed Locks to all cron tasks.
3.  **Secure Bot**: Add `telegram_bot` middleware to check `user_id` vs `admin_list`.

### Phase 2: Visibility (The "See What's Happening" Update)
1.  **Observability Stack**: Add Grafana/Loki to Docker Compose.
2.  **Dashboards**: Create a basic dashboard for "Active Agents", "Error Rate", "Server Health".

### Phase 3: Deployment (The "Real World" Update)
1.  **DevOps Node**: Implement Ansible runners.
2.  **Secrets Management**: Implement SOPS for managing real production secrets (API keys for the *generated* apps).

## Code Analysis Specifics

| Component | Status | Issue |
|-----------|--------|-------|
| `langgraph` | ⚠️ Risky | Uses `MemorySaver`. Logic is good, persistence is non-existent. |
| `scheduler` | ⚠️ Risky | No locking. `_ingest_to_rag` is "fire-and-forget" with no retry queue. |
| `api` | ⚠️ Open | No auth middleware. Relies entirely on network isolation. |
| `telegram_bot` | 🚨 Insecure | No user filtering. Logic is tightly coupled to API. |
| `shared` | ✅ Good | Models and schemas are well structured. |
