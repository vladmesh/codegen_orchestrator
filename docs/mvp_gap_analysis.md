# MVP Gap Analysis & Critical Roadmap

**Last Updated:** 2025-12-28

## Executive Summary

Codegen Orchestrator имеет рабочий прототип "Happy Path", способный создавать GitHub репозитории и синхронизировать их. Однако отсутствуют необходимые слои **Resilience**, **Security** и **Operations** для стабильного MVP.

**Current State:** Proof-of-Concept / Prototype  
**Target State:** Stable, Secure, and Deployable MVP

---

## 1. 🚨 Critical Blockers (Must Fix)

_All critical blockers resolved._

## 1.5 Previously Critical (Now Resolved)

### 1.1 Worker Image Build ✅ RESOLVED
- **Was**: `coding-worker:latest` не собирался автоматически
- **Status**: FIXED — добавлено в `make build` и отдельный `make build-coding-worker`
- **Location**: `Makefile:73-78`

---

## 2. 🔐 Security & Secrets

### 2.1 Secret Management ❌
- **Problem**: Секреты хранятся в plaintext с TODO комментариями
- **Location**: `services/api/src/routers/api_keys.py:36-37, 72-73`
- **Evidence**: `# TODO: Add real encryption here` + `encrypted_value = key_value`
- **Fix**: Реализовать SOPS/AGE или database-level encryption

### 2.2 API Authentication ❌
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

### State Management / MemorySaver ✅
- **Was**: "MemorySaver теряет состояние при рестарте"
- **Status**: FIXED — реализовано ручное хранение сообщений в PostgreSQL через RAG
- **Note**: Решено использовать custom persistence вместо langgraph-checkpoint-postgres

### Telegram Access Control ✅
- **Was**: "Middleware при пустом whitelist пропускает всех (fail-open)"
- **Status**: FIXED — реализована двухуровневая авторизация:
  1. Админы (из `ADMIN_TELEGRAM_IDS` env) → полный доступ
  2. Пользователи из БД (созданные админом) → базовый доступ
  3. Остальные → блокировка (fail-closed)
- **Features**: Серверы и чужие проекты скрыты для обычных пользователей
- **Location**: `services/telegram_bot/src/middleware.py`, `handlers.py`, `keyboards.py`

---

## Recommended Roadmap

### Phase 1: Security (Critical)
| Priority | Task | Effort |
|----------|------|--------|
| P1 | Add Redis locks to Scheduler (3.1) | 2h |
| P1 | API authentication middleware (2.2) | 2-4h |
| P1 | Implement secret encryption (2.1) | 4h |

### Phase 2: Stabilization
| Priority | Task | Effort |
|----------|------|--------|
| P1 | Update Ansible for full .env (4.1, 4.2) | 2h |

### Phase 3: Operations
| Priority | Task | Effort |
|----------|------|--------|
| P2 | Add observability stack (5.1) | 4h |
| P2 | Update documentation (5.2) | 1h |
