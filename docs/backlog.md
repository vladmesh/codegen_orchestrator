# Backlog

## Фаза 0: Foundation

### Поднять инфраструктуру

**Status:** TODO
**Priority:** HIGH

Базовая инфраструктура для разработки оркестратора.

**Tasks:**
- [ ] `cp .env.example .env` и заполнить переменные
- [ ] `make build && make up`
- [ ] `make migrate` — создать таблицы в БД
- [ ] Проверить что API отвечает на `/health`

---

### Установить Sysbox на сервер оркестратора

**Status:** DONE
**Priority:** HIGH

Для параллельных workers нужен Sysbox runtime.

**Tasks:**
- [x] Скачать и установить Sysbox CE
- [x] Проверить `docker info | grep sysbox`
- [x] Протестировать запуск nested Docker

**Docs:** https://github.com/nestybox/sysbox

---

### Настроить SOPS + AGE для секретов

**Status:** TODO
**Priority:** HIGH

Шифрование secrets.yaml для хранения токенов и ключей.

**Tasks:**
- [ ] Установить SOPS и AGE
- [ ] Сгенерировать AGE ключ
- [ ] Создать secrets.yaml с тестовыми данными
- [ ] Проверить шифрование/дешифрование

---

## Фаза 1: Вертикальный слайс

### Минимальный Telegram → LangGraph flow

**Status:** TODO
**Priority:** HIGH

Пользователь пишет в Телеграм, получает ответ от LangGraph.

**Tasks:**
- [ ] Создать Telegram бота через @BotFather
- [ ] Прописать токен в `.env`
- [ ] Реализовать передачу сообщений из бота в LangGraph
- [ ] Реализовать отправку ответа обратно в Телеграм

**Open questions:**
- Как хранить thread_id для пользователя? (Redis? Postgres?)

---

### Brainstorm → Architect flow

**Status:** DONE
**Priority:** MEDIUM

Брейнсторм создаёт спецификацию, Архитектор генерирует проект.

**Tasks:**
- [x] Реализовать brainstorm node с LLM
- [x] Определить формат project_spec
- [x] Реализовать architect node с Factory.ai
- [x] GitHub App для создания репозиториев

---

### Zavhoz: выдача ресурсов

**Status:** DONE
**Priority:** MEDIUM

Завхоз выдаёт handles для ресурсов, не раскрывая секреты LLM.

**Tasks:**
- [x] Модель Resource в API (уже есть базовая)
- [x] Эндпоинты: allocate, get, list
- [ ] Интеграция с SOPS для чтения реальных секретов
- [x] Tool для LangGraph: request_resource

---

## Фаза 2: Параллельные Workers

### Worker Docker Image

**Status:** DONE
**Priority:** MEDIUM

Образ с git, gh CLI, Factory.ai для выполнения coding tasks.

**Tasks:**
- [x] Dockerfile на базе Ubuntu 22.04
- [x] Установить git, gh, Factory.ai Droid CLI
- [x] Скрипт execute_task.sh
- [x] Протестировать с Sysbox runtime

---

### Worker Spawner Microservice

**Status:** DONE
**Priority:** HIGH

Микросервис для изоляции Docker API от LangGraph.

**Tasks:**
- [x] Redis pub/sub коммуникация
- [x] `worker:spawn` / `worker:result:{id}` каналы
- [x] Docker socket mount
- [x] Client library для LangGraph

---

### Parallel Developer Node

**Status:** TODO
**Priority:** MEDIUM

Узел графа для параллельного запуска coding workers.

**Tasks:**
- [ ] spawn_sysbox_worker function
- [ ] asyncio.gather для параллельного запуска
- [ ] Парсинг результатов (PR URL, статус)
- [ ] Обработка ошибок

---

### Reviewer Node

**Status:** TODO  
**Priority:** MEDIUM

Ревью и merge PR через gh CLI.

**Tasks:**
- [ ] gh pr diff для получения изменений
- [ ] LLM для code review
- [ ] gh pr merge или gh pr comment
- [ ] Логика возврата на доработку

---

## Фаза 3: DevOps Integration

### DevOps Node + prod_infra

**Status:** TODO
**Priority:** LOW

Интеграция с Ansible для деплоя.

**Tasks:**
- [ ] Wrapper над ansible-playbook
- [ ] Обновление services.yml
- [ ] DNS через Cloudflare API
- [ ] Health check после деплоя

**Open questions:**
- Как передать SSH ключ агенту? (через Завхоза?)
- Как обрабатывать ошибки Ansible?

---

## Ideas / Future

### Cost Tracking

Отслеживание расходов на LLM.

**Ideas:**
- Логировать tokens per request
- Агрегировать по проектам
- Алерты при превышении бюджета

---

### Human Escalation

Когда просить помощи у человека.

**Triggers:**
- Агент застрял > N итераций
- Ошибка без recovery
- Финансовые решения (покупка домена, сервера)
- Merge в main с breaking changes

---

### Multi-tenancy

Несколько пользователей / проектов.

**Questions:**
- Разные Telegram пользователи = разные threads?
- Изоляция ресурсов между проектами?
- Квоты на LLM usage?

---

### CLI Interface

Альтернативный интерфейс помимо Telegram.

```bash
# Идея
orchestrator new "Weather bot with notifications"
orchestrator status
orchestrator deploy
```

---

---

### Advanced Model Management & Dashboard

**Status:** TODO
**Priority:** MEDIUM

Support for late 2025 SOTA models (gpt-5.2, Gemini 3 Pro, Claude Opus 4.5) and dynamic runtime configuration.

**Tasks:**
- [ ] Database schema for storing Model Configs (provider, model_name, api_key_ref, temperature, prompt_templates).
- [ ] Admin Dashboard (Web UI) for managing these configs at runtime.
- [ ] Dynamic LLM factory that reads from DB instead of envs.
- [ ] Support for high-end models: `gpt-5.2`, `google/gemini-3-pro`, `anthropic/claude-opus-4.5`.

## Done

- **Sysbox Installation** - Installed on dev machine
- **Worker Docker Image** - `coding-worker:latest` with Factory.ai
- **Worker Spawner** - Redis pub/sub microservice
- **Architect Node** - Creates GitHub repos, spawns Factory workers
- **GitHub App Integration** - Auto-detects org, creates repos
- **Brainstorm → Zavhoz → Architect flow** - Tested end-to-end

