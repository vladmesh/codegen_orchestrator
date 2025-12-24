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

**Status:** TODO
**Priority:** HIGH

Для параллельных workers нужен Sysbox runtime.

**Tasks:**
- [ ] Скачать и установить Sysbox CE
- [ ] Проверить `docker info | grep sysbox`
- [ ] Протестировать запуск nested Docker

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

**Status:** TODO
**Priority:** MEDIUM

Брейнсторм создаёт спецификацию, Архитектор генерирует проект.

**Tasks:**
- [ ] Реализовать brainstorm node с LLM
- [ ] Определить формат project_spec
- [ ] Реализовать architect node с вызовом Copier
- [ ] Тестовый прогон: "Создай бота для погоды"

**Open questions:**
- Где хранить сгенерированный проект? (локально? GitHub сразу?)

---

### Zavhoz: выдача ресурсов

**Status:** TODO
**Priority:** MEDIUM

Завхоз выдаёт handles для ресурсов, не раскрывая секреты LLM.

**Tasks:**
- [ ] Модель Resource в API (уже есть базовая)
- [ ] Эндпоинты: allocate, get, list
- [ ] Интеграция с SOPS для чтения реальных секретов
- [ ] Tool для LangGraph: request_resource

---

## Фаза 2: Параллельные Workers

### Worker Docker Image

**Status:** TODO
**Priority:** MEDIUM

Образ с git, gh CLI, Claude Code для выполнения coding tasks.

**Tasks:**
- [ ] Dockerfile на базе nestybox/ubuntu-jammy-systemd-docker
- [ ] Установить git, gh, nodejs, claude-code
- [ ] Скрипт execute_task.sh
- [ ] Протестировать с Sysbox runtime

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

*Пока пусто*
