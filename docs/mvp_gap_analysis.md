# MVP Gap Analysis & Critical Roadmap

**Last Updated:** 2025-01-27

## Executive Summary

Codegen Orchestrator имеет рабочий прототип "Happy Path", способный создавать GitHub репозитории и синхронизировать их. Однако отсутствуют необходимые слои **Resilience**, **Security** и **Operations** для стабильного MVP.

**Current State:** Proof-of-Concept / Prototype  
**Target State:** Stable, Secure, and Deployable MVP

---

## 1. 🚨 Critical Blockers (Must Fix)

### 1.1 Resilience & State Management ❌
- **Problem**: `services/langgraph/src/graph.py:387` использует `MemorySaver`
- **Impact**: Рестарт контейнера `langgraph` уничтожает ВСЕ conversation threads и состояния процессов
- **Location**: `graph.py:387-388`
- **Fix**: Интегрировать `langgraph-checkpoint-postgres` для персистенции в PostgreSQL

### 1.2 Developer Node Instability ❌
- **Problem**: `request_spawn()` возвращает `SpawnResult` dataclass, но `developer.py:117` обращается к нему как к dict через `.get()`
- **Impact**: `AttributeError` при попытке запустить coding worker
- **Location**: `services/langgraph/src/nodes/developer.py:117`
- **Fix**: Заменить `worker_result.get("success")` на `worker_result.success`

### 1.3 API Path Mismatch ❌
- **Problem**: `InternalAPIClient` использует `base_url=http://api:8000` и делает запросы на `/projects/`, но API роутеры подключены с `prefix="/api"` → реальный путь `/api/projects/`
- **Impact**: 404 ошибки при любых API вызовах из LangGraph tools
- **Location**: `services/langgraph/src/tools/base.py:23`, `services/api/src/main.py:89`
- **Fix**: Изменить base_url на `http://api:8000/api` или обновить все пути в tools

### 1.4 Worker Image Build ⚠️
- **Problem**: `coding-worker:latest` не собирается автоматически через docker-compose
- **Impact**: На чистой машине worker spawning падает
- **Location**: `services/coding-worker/Dockerfile` (существует, но не в compose)
- **Fix**: Добавить в Makefile команду `build-worker` или документировать manual build

---

## 2. 🔐 Security & Secrets

### 2.1 Telegram Access Control ❌
- **Problem**: Бот принимает сообщения от ЛЮБОГО пользователя без whitelist
- **Impact**: Неавторизованный доступ к ресурсам и проектам
- **Location**: `services/telegram_bot/src/main.py`
- **Fix**: Добавить `ALLOWED_USER_IDS` middleware

### 2.2 Secret Management ❌
- **Problem**: Секреты хранятся в plaintext с TODO комментариями
- **Location**: `services/api/src/routers/api_keys.py:36-37, 72-73`
- **Evidence**: `# TODO: Add real encryption here` + `encrypted_value = key_value`
- **Fix**: Реализовать SOPS/AGE или database-level encryption

### 2.3 API Authentication ❌
- **Problem**: Нет Auth/ACL на API endpoints
- **Impact**: Полагается только на network isolation
- **Fix**: Добавить authentication middleware

---

## 3. 🧩 Architecture

### 3.1 Scheduler Race Conditions ❌
- **Problem**: `services/scheduler/src/main.py` запускает workers через `asyncio.gather()` без distributed locking
- **Impact**: Несколько реплик = дублирование действий
- **Fix**: Добавить Redis distributed locks для всех background tasks

---

## 4. 🚀 DevOps & Deployment

### 4.1 Ansible Playbook Limitations ⚠️
- **Problem**: `deploy_project.yml` пишет только `PORT` в `.env`
- **Impact**: Проекты не получают необходимые secrets (DB passwords, API keys)
- **Location**: `services/infrastructure/ansible/playbooks/deploy_project.yml:27-29`
- **Fix**: Генерировать полный `.env` из project config

### 4.2 Insecure Docker Login ⚠️
- **Problem**: GitHub token передаётся через echo pipe в docker login
- **Location**: `deploy_project.yml:34`
- **Fix**: Использовать `docker login --password-stdin` с proper stdin handling

---

## 5. 👁️ Observability & Docs

### 5.1 Missing Observability Stack ⚠️
- **Problem**: Нет Prometheus, Loki, Grafana в `docker-compose.yml`
- **Fix**: Добавить observability stack и настроить structlog для ship logs

### 5.2 Documentation Drift ⚠️
- **Problem**: 
  - `ARCHITECTURE.md:126` упоминает "Brainstorm" node (удалён, заменён на Analyst)
  - `docs/NODES.md`, `product_owner_design.md` ссылаются на Brainstorm
- **Fix**: Обновить документацию, заменить Brainstorm → Analyst

---

## ✅ Resolved Issues (Removed from Gap Analysis)

### RAG Scoping ✅
- **Was**: "RAG operates in scope=public"
- **Status**: FIXED — реализован полноценный scope-based filtering с `user_id` и `project_id`
- **Location**: `services/api/src/routers/rag.py:96-141, 333-392`

### User-Project Binding ✅
- **Was**: "No explicit binding between Telegram messages and Projects"
- **Status**: FIXED — `project_id` передаётся в RAG через API endpoints и LangGraph tools
- **Location**: `services/langgraph/src/tools/rag.py:21`

### Schema & State Conflicts ✅
- **Was**: "ProjectStatus enums conflict"
- **Status**: NOT AN ISSUE — `ProjectStatus` и `ServerStatus` чётко определены без конфликтов
- **Location**: `shared/models/project.py:11-40`, `shared/models/server.py:12-34`

### DevOps Node Placeholder ✅
- **Was**: "DevOps node is a placeholder"
- **Status**: FIXED — полностью реализован с Ansible integration
- **Location**: `services/langgraph/src/nodes/devops.py` (251 lines)

---

## Recommended Roadmap

### Phase 1: Stabilization (Critical)
| Priority | Task | Effort |
|----------|------|--------|
| P0 | Fix API path mismatch (1.3) | 30min |
| P0 | Fix Developer node dataclass access (1.2) | 15min |
| P1 | Implement Postgres Checkpointer (1.1) | 2-4h |
| P1 | Add Redis locks to Scheduler (3.1) | 2h |
| P2 | Document/automate coding-worker build (1.4) | 30min |

### Phase 2: Security
| Priority | Task | Effort |
|----------|------|--------|
| P0 | Telegram user whitelist (2.1) | 1h |
| P1 | API authentication middleware (2.3) | 2-4h |
| P1 | Implement secret encryption (2.2) | 4h |

### Phase 3: Operations
| Priority | Task | Effort |
|----------|------|--------|
| P1 | Update Ansible for full .env (4.1, 4.2) | 2h |
| P2 | Add observability stack (5.1) | 4h |
| P2 | Update documentation (5.2) | 1h |
