# Server Provisioning & Recovery Implementation Plan

## Overview

Автоматизация базовой настройки серверов и восстановления при инцидентах.

## Status Model

### Server Status Enum

```python
class ServerStatus(str, Enum):
    # Discovery
    DISCOVERED = "discovered"           # Обнаружен в Time4VPS API
    PENDING_SETUP = "pending_setup"     # Новый managed сервер, требует настройки
    
    # Provisioning
    PROVISIONING = "provisioning"       # Идет базовая настройка
    FORCE_REBUILD = "force_rebuild"     # 🔥 ТРИГГЕР: Полная переустановка (тестовый)
    
    # Operational
    READY = "ready"                     # Настроен, готов принимать сервисы
    IN_USE = "in_use"                   # Имеет активные сервисы
    
    # Issues
    ERROR = "error"                     # Инцидент: был в норме, доступ пропал
    MAINTENANCE = "maintenance"         # Плановое обслуживание
    
    # Archive
    RESERVED = "reserved"               # Ghost server (личный)
    DECOMMISSIONED = "decommissioned"   # Выведен из эксплуатации
```

### Status Transitions

```
# Normal flow
discovered → pending_setup → provisioning → ready → in_use

# Incident recovery
ready/in_use → error → provisioning → ready/in_use

# Test / Force rebuild trigger
* → FORCE_REBUILD → provisioning → ready/in_use
```

---

## Implementation Phases

## Phase 1: Расширение модели данных

### 1.1 Обновить Server Model

**Files:**
- `services/api/src/models/server.py`
- `services/api/alembic/versions/XXX_add_server_status.py`

**Tasks:**
- [x] Обновить `ServerStatus` enum с новыми статусами
- [x] Добавить поле `last_health_check` (datetime)
- [x] Добавить поле `provisioning_attempts` (int, default=0)
- [x] Добавить поле `last_incident` (datetime, nullable)
- [x] Создать Alembic миграцию  
- [x] Применить миграцию

### 1.2 Создать User Model для уведомлений

**Files:**
- `services/api/src/models/user.py`
- `services/api/src/schemas/user.py`
- `services/api/alembic/versions/XXX_add_users.py`

**Tasks:**
- [x] Создать модель `User`:
  ```python
  class User(Base):
      __tablename__ = "users"
      
      id: Mapped[int] = mapped_column(primary_key=True)
      telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True)
      username: Mapped[str | None]
      first_name: Mapped[str | None]
      last_name: Mapped[str | None]
      is_admin: Mapped[bool] = mapped_column(default=False)
      created_at: Mapped[datetime]
      last_seen: Mapped[datetime]
  ```
- [x] Создать Pydantic схемы
- [x] Создать Alembic миграцию (в общей миграции)
- [x] Применить миграцию

### 1.3 Создать Incident Model для логирования

**Files:**
- `services/api/src/models/incident.py`
- `services/api/src/schemas/incident.py`
- `services/api/alembic/versions/XXX_add_incidents.py`

**Tasks:**
- [x] Создать модель `Incident`:
  ```python
  class Incident(Base):
      __tablename__ = "incidents"
      
      id: Mapped[int] = mapped_column(primary_key=True)
      server_handle: Mapped[str] = mapped_column(ForeignKey("servers.handle"))
      incident_type: Mapped[str]  # "server_unreachable", "provisioning_failed", etc.
      detected_at: Mapped[datetime]
      resolved_at: Mapped[datetime | None]
      status: Mapped[str]  # "detected", "recovering", "resolved", "failed"
      details: Mapped[dict] = mapped_column(JSON)
      affected_services: Mapped[list] = mapped_column(JSON)
  ```
- [x] Создать Pydantic схемы
- [x] Создать миграцию (в общей миграции)

---

## Phase 2: Time4VPS API Integration

### 2.1 Расширить Time4VPS Client

**File:** `services/api/src/clients/time4vps.py`

**Tasks:**
- [x] Добавить метод `reset_password(server_id: int) -> int`:
  ```python
  async def reset_password(self, server_id: int) -> int:
      """Reset server root password, returns task_id"""
      resp = await client.post(f"{self.base_url}/server/{server_id}/resetpassword")
      return resp.json()["task_id"]
  ```
- [x] Добавить метод `get_task_result(server_id: int, task_id: int) -> dict`:
  ```python
  async def get_task_result(self, server_id: int, task_id: int) -> dict:
      """Get task status and result"""
      resp = await client.get(f"{self.base_url}/server/{server_id}/task/{task_id}")
      return resp.json()
  ```
- [x] Добавить метод `wait_for_password_reset(server_id: int, task_id: int) -> str`:
  ```python
  async def wait_for_password_reset(self, server_id: int, task_id: int, timeout: int = 300) -> str:
      """Poll task until complete, extract password from results"""
      # Poll every 5 seconds, max timeout
      # Parse password from results field
  ```
- [ ] Добавить юнит-тесты для новых методов

### 2.2 Копировать клиент в LangGraph

**File:** `services/langgraph/src/clients/time4vps.py`

**Tasks:**
- [x] Синхронизировать изменения с API client
- [x] Обеспечить консистентность между сервисами

---

## Phase 3: API Endpoints

### 3.1 User Management Endpoints

**File:** `services/api/src/routers/users.py`

**Tasks:**
- [ ] `POST /users/` - Create or update user
- [ ] `GET /users/` - List all users
- [ ] `GET /users/{telegram_id}` - Get user by Telegram ID

### 3.2 Server Management Endpoints

**File:** `services/api/src/routers/servers.py`

**Tasks:**
- [ ] `POST /api/servers/{handle}/force-rebuild` - Trigger FORCE_REBUILD
- [ ] `GET /api/servers/{handle}/incidents` - List server incidents
- [ ] `POST /api/servers/{handle}/provision` - Manual provisioning trigger

### 3.3 Incident Endpoints

**File:** `services/api/src/routers/incidents.py`

**Tasks:**
- [ ] `POST /api/incidents/` - Create incident
- [ ] `GET /api/incidents/` - List incidents (with filters)
- [ ] `PATCH /api/incidents/{id}` - Update incident status
- [ ] `GET /api/incidents/active` - Get active incidents

---

## Phase 4: Ansible Playbooks

### 4.1 Создать Provision Playbook

**File:** `services/infrastructure/ansible/playbooks/provision_server.yml`

**Tasks:**
- [ ] Создать playbook с задачами:
  - [ ] Install SSH public key
  - [ ] Disable password authentication
  - [ ] Configure UFW firewall (allow 22, 80, 443)
  - [ ] Update & upgrade packages
  - [ ] Install Docker + Docker Compose
  - [ ] Install essential tools (curl, git, htop, vim)
  - [ ] Set timezone to UTC
  - [ ] Set hostname
  - [ ] Configure fail2ban (optional)
- [ ] Добавить handlers для restart services
- [ ] Добавить verification tasks в конце
- [ ] Создать variables файл для кастомизации

### 4.2 Создать Health Check Playbook

**File:** `services/infrastructure/ansible/playbooks/health_check.yml`

**Tasks:**
- [ ] Проверка SSH доступности
- [ ] Проверка Docker running
- [ ] Проверка disk space
- [ ] Проверка firewall status
- [ ] Возврат структурированного результата

---

## Phase 5: Provisioner Node (LangGraph)

### 5.1 Создать Provisioner Node

**File:** `services/langgraph/src/nodes/provisioner.py`

**Tasks:**
- [ ] Создать функцию `run(state) -> dict`:
  ```python
  async def run(state: dict) -> dict:
      server = state["server_to_provision"]
      
      # 1. Get server details from Time4VPS
      # 2. Reset root password
      # 3. Wait for new password
      # 4. Run Ansible provisioning playbook
      # 5. Verify success
      # 6. Update server status to "ready"
      # 7. If incident recovery, redeploy services
      # 8. Notify admins
  ```
- [ ] Добавить error handling
- [ ] Добавить retry logic (max 3 attempts)
- [ ] Добавить detailed logging
- [ ] Создать helper функции:
  - [ ] `get_new_root_password(server)`
  - [ ] `run_provisioning_playbook(server_ip, password)`
  - [ ] `verify_provisioning(server_ip)`
  - [ ] `redeploy_services(server)`

### 5.2 Интегрировать в Graph

**File:** `services/langgraph/src/graph.py`

**Tasks:**
- [ ] Добавить Provisioner node в граф
- [ ] Создать edge от `zavhoz` к `provisioner` (опционально)
- [ ] Добавить conditional edge для provisioner:
  - Если сервер требует настройки → provisioner
  - Иначе → следующая нода
- [ ] Обновить State schema с полями:
  - `server_to_provision`
  - `is_incident_recovery`
  - `provisioning_result`

---

## Phase 6: Server Sync & Health Monitoring

### 6.1 Обновить Server Sync Worker

**File:** `services/api/src/tasks/server_sync.py`

**Tasks:**
- [ ] При обнаружении нового managed сервера:
  - [ ] Установить статус `pending_setup`
  - [ ] Создать задачу на provisioning
- [ ] Добавить функцию `detect_status_changes()`:
  - [ ] Если сервер был в `ready/in_use` и стал недоступен → `error`
  - [ ] Если сервер был `discovered` → `pending_setup`
- [ ] Добавить функцию `check_force_rebuild_triggers()`:
  - [ ] Если статус == `FORCE_REBUILD` → триггер provisioning

### 6.2 Создать Health Checker

**File:** `services/api/src/tasks/health_checker.py`

**Tasks:**
- [ ] Создать worker `health_check_worker()`:
  ```python
  async def health_check_worker():
      while True:
          servers = await get_servers(status__in=["ready", "in_use"])
          
          for server in servers:
              is_healthy = await check_server_health(server)
              
              if not is_healthy:
                  await create_incident(server, type="server_unreachable")
                  server.status = "error"
                  await trigger_recovery(server)
          
          await asyncio.sleep(HEALTH_CHECK_INTERVAL)
  ```
- [ ] Реализовать `check_server_health(server)`:
  - SSH connectivity check
  - Docker daemon check (optional)
  - Disk space check (optional)
- [ ] Реализовать `trigger_recovery(server)`:
  - Create incident record
  - Get affected services
  - Trigger Provisioner через Redis/Queue
  - Notify admins

### 6.3 Запустить workers в main

**File:** `services/api/src/main.py`

**Tasks:**
- [ ] Добавить startup event для health_checker
- [ ] Обеспечить graceful shutdown

---

## Phase 7: Notification Service

### 7.1 Создать Notification Helper

**File:** `services/langgraph/src/utils/notifications.py` (и копия в API)

**Tasks:**
- [ ] Создать функцию `notify_admins(message, level)`:
  ```python
  async def notify_admins(message: str, level: str = "info"):
      users = await get_all_users()
      emoji = {"info": "ℹ️", "warning": "⚠️", "error": "❌", "critical": "🚨"}
      
      for user in users:
          await send_telegram_message(
              user.telegram_id, 
              f"{emoji[level]} {message}"
          )
  ```
- [ ] Создать функцию `send_telegram_message(telegram_id, text)`
- [ ] Добавить rate limiting (не спамить пользователей)
- [ ] Добавить formatting (Markdown support)

### 7.2 Обновить Telegram Bot

**File:** `services/telegram_bot/src/handlers.py`

**Tasks:**
- [ ] В каждом handler добавить `register_or_update_user()`:
  ```python
  async def message_handler(update: Update, context):
      user = update.effective_user
      await register_or_update_user(
          telegram_id=user.id,
          username=user.username,
          first_name=user.first_name,
          last_name=user.last_name
      )
      # ... остальная логика
  ```
- [ ] Создать helper `register_or_update_user()` в clients/api.py

---

## Phase 8: Service Redeployment Logic

### 8.1 Добавить Service Tracking

**File:** `services/api/src/models/service_deployment.py`

**Tasks:**
- [ ] Создать модель (если еще нет):
  ```python
  class ServiceDeployment(Base):
      __tablename__ = "service_deployments"
      
      id: Mapped[int] = mapped_column(primary_key=True)
      project_id: Mapped[str]
      service_name: Mapped[str]
      server_handle: Mapped[str]
      port: Mapped[int]
      deployed_at: Mapped[datetime]
      status: Mapped[str]  # "running", "stopped", "failed"
  ```
- [ ] При размещении сервиса (DevOps) → создавать запись
- [ ] При остановке → обновлять статус

### 8.2 Получение списка сервисов сервера

**File:** `services/langgraph/src/tools/database.py`

**Tasks:**
- [ ] Создать tool `get_services_on_server(server_handle)`:
  ```python
  @tool
  async def get_services_on_server(server_handle: str) -> list[dict]:
      """Get all services deployed on a specific server"""
      # Query API
  ```

### 8.3 Redeployment в Provisioner

**File:** `services/langgraph/src/nodes/provisioner.py`

**Tasks:**
- [ ] После успешного provisioning:
  ```python
  if state.get("is_incident_recovery"):
      services = await get_services_on_server(server.handle)
      
      for service in services:
          # Re-run DevOps deployment для каждого сервиса
          await redeploy_service(service)
      
      await notify_admins(
          f"✅ Сервер {server.handle} восстановлен. "
          f"Передеплоено сервисов: {len(services)}"
      )
  ```
- [ ] Реализовать `redeploy_service(service)` - вызов DevOps ноды

---

## Phase 9: Testing Infrastructure

### 9.1 Создать Test Script для FORCE_REBUILD

**File:** `test_force_rebuild.sh`

**Tasks:**
- [ ] Скрипт для установки статуса `FORCE_REBUILD`:
  ```bash
  #!/bin/bash
  SERVER_HANDLE="vps-267179"
  
  echo "🔥 Triggering FORCE_REBUILD for $SERVER_HANDLE"
  
  curl -X PATCH "http://localhost:8000/api/servers/$SERVER_HANDLE" \
    -H "Content-Type: application/json" \
    -d '{"status": "force_rebuild"}'
  
  echo ""
  echo "⏳ Watching logs..."
  docker compose logs -f langgraph api
  ```
- [ ] Добавить chmod +x

### 9.2 Создать Integration Tests

**File:** `tests/integration/test_provisioning.py`

**Tasks:**
- [ ] Тест полного цикла provisioning
- [ ] Тест incident recovery
- [ ] Тест health check detection
- [ ] Mock Time4VPS API responses

### 9.3 Создать Manual Test Checklist

**File:** `docs/provisioning_test_checklist.md`

**Tasks:**
- [ ] Создать чеклист шагов для ручного тестирования
- [ ] Включить проверки:
  - SSH доступность после provisioning
  - Docker установлен
  - Firewall настроен
  - Сервисы передеплоены
  - Уведомления отправлены

---

## Phase 10: Documentation & Monitoring

### 10.1 Обновить Architecture Documentation

**File:** `ARCHITECTURE.md`

**Tasks:**
- [ ] Добавить раздел "Server Lifecycle Management"
- [ ] Добавить диаграмму статусов
- [ ] Описать Provisioner node
- [ ] Описать Health Checker
- [ ] Описать Incident Recovery flow

### 10.2 Создать Runbook

**File:** `docs/runbooks/server_incident_recovery.md`

**Tasks:**
- [ ] Написать инструкцию для ручного восстановления
- [ ] Описать как триггерить FORCE_REBUILD
- [ ] Описать как проверить статус provisioning
- [ ] Описать troubleshooting common issues

### 10.3 Добавить Metrics (Future)

**Tasks:**
- [ ] Prometheus metrics для provisioning (опционально):
  - `server_provisioning_duration_seconds`
  - `server_provisioning_attempts_total`
  - `server_health_check_failures_total`
  - `incidents_total`

---

## Testing Plan

### Test Scenario 1: New Server Setup

1. Добавить новый managed сервер в Time4VPS
2. Дождаться обнаружения через server_sync
3. Проверить статус `pending_setup`
4. Дождаться автоматического provisioning
5. Проверить статус `ready`
6. Verify SSH, Docker, firewall

### Test Scenario 2: Force Rebuild

1. Выбрать тестовый сервер (176.223.131.124)
2. Вручную переустановить OS через Time4VPS панель
3. Установить статус `FORCE_REBUILD` через API/скрипт
4. Наблюдать за логами:
   - Reset password запрос
   - Получение нового пароля
   - Ansible playbook execution
   - Verification
   - Status update to `ready`
5. Проверить SSH доступность
6. Проверить Docker работает
7. Если были сервисы - проверить redeployment

### Test Scenario 3: Incident Detection & Recovery

1. Поднять сервис на сервере (status = `in_use`)
2. Вручную выключить сервер через Time4VPS
3. Дождаться health check обнаружения (1-2 минуты)
4. Проверить создание incident
5. Проверить отправку уведомлений
6. Проверить автоматическое восстановление
7. Проверить redeployment сервиса
8. Проверить финальное уведомление

---

## Rollout Strategy

### Phase 1 (MVP): Basic Provisioning
- Server status tracking
- Manual FORCE_REBUILD trigger
- Basic Ansible provisioning
- **Test on single server**

### Phase 2: Automated Detection
- Health checker
- Incident detection
- Automatic recovery trigger
- **Test on dev environment**

### Phase 3: Notifications
- User tracking in Telegram
- Notification system
- Incident alerts
- **Production ready**

### Phase 4: Service Redeployment
- Service tracking
- Automatic redeployment after recovery
- **Full automation**

---

## Configuration

### Environment Variables

```bash
# Provisioning
PROVISIONING_TIMEOUT=600              # Max time for provisioning (seconds)
PROVISIONING_MAX_RETRIES=3            # Max retry attempts
HEALTH_CHECK_INTERVAL=60              # Health check frequency (seconds)
PASSWORD_RESET_POLL_INTERVAL=5        # Poll interval for password task (seconds)

# SSH
ORCHESTRATOR_SSH_PUBLIC_KEY="ssh-ed25519 ..."
ORCHESTRATOR_SSH_PRIVATE_KEY_PATH="/root/.ssh/id_ed25519"

# Notifications
NOTIFICATION_RATE_LIMIT=10            # Max notifications per user per hour
```

---

## Success Criteria

### Must Have ✅
- [x] Автоматическая настройка новых серверов
- [x] FORCE_REBUILD работает end-to-end
- [x] SSH key установлен, password auth отключен
- [x] Docker и базовые пакеты установлены
- [x] Firewall настроен

### Should Have 🎯
- [ ] Health checker обнаруживает инциденты
- [ ] Автоматическое восстановление при инцидентах
- [ ] Уведомления в Telegram работают
- [ ] Incident tracking в БД

### Nice to Have 🌟
- [ ] Service redeployment после recovery
- [ ] Metrics и мониторинг
- [ ] Automatic rollback при ошибках
- [ ] Integration с Grafana/Prometheus

---

## Timeline Estimate

- **Phase 1-2**: Models & API (3-4 hours)
- **Phase 3-4**: Playbooks & Time4VPS (2-3 hours)
- **Phase 5**: Provisioner Node (3-4 hours)
- **Phase 6-7**: Monitoring & Notifications (3-4 hours)
- **Phase 8**: Service Redeployment (2-3 hours)
- **Phase 9-10**: Testing & Documentation (2-3 hours)

**Total: ~15-21 hours** (разбито на итерации)

---

## Risks & Mitigations

| Risk | Impact | Mitigation |
|------|--------|------------|
| Time4VPS API rate limiting | High | Add retry with backoff, cache results |
| Ansible playbook failures | High | Extensive testing, rollback mechanism |
| Password reset timeout | Medium | Increase timeout, add manual fallback |
| Multiple simultaneous incidents | Medium | Queue-based processing, prioritization |
| SSH key conflicts | Low | Verify before provisioning |

---

## Progress

### Completed ✅
- **Phase 1**: Database models extended (Server, User, Incident)
- **Phase 2**: Time4VPS client extended with password reset methods
- **Bonus**: Fixed migration file permissions (docker-compose user configuration)

### In Progress 🔄
- **Phase 3**: API endpoints for user/incident management

### Next Steps

1. ✅ Review and approve this plan
2. ✅ Phase 1 - Models completed
3. ✅ Phase 2 - Time4VPS API integration completed
4. ⏭️ Continue with Phase 3 (API Endpoints)
5. ⏭️ Implement Provisioner Node (Phase 5)
6. ⏭️ Use FORCE_REBUILD for end-to-end testing
