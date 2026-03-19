# Post-Release QA MVP — Claude Code on Prod Servers

> [!WARNING]
> Этот файл автогенерируется командой `make sync`. Не редактируйте вручную — изменения будут перезаписаны.

# Brainstorm: Post-Release QA MVP — Claude Code on Prod Servers

> **Дата**: 2026-03-16
> **Контекст**: После деплоя нет проверки кроме `/health` 200. Нужен полноценный QA-шаг перед тем как считать стори завершённой.
> **Status**: done
> **Связано с**: [qa-node.md](qa-node.md) (архитектура QA), [qa-runner-on-prod-server.md](qa-runner-on-prod-server.md) (где запускать)

---

## Current State

Deploy consumer после успешного деплоя:
1. SmokeTester проверяет `/health` → 200 (для бэкендов) и `/start` через Telethon (для ботов, но **всегда skipped** — env vars не настроены)
2. `_handle_deploy_success()` → story = `completed` → notify user → delete worker container
3. Юзер получает ссылку и сам проверяет

**Проблемы**:
- `/health` 200 не гарантирует что бизнес-логика работает (weather_bot: `/health` OK, `/api/weather/moscow` 404)
- tg_bot smoke **никогда не работает** — Telethon не настроен
- Нет проверки acceptance criteria — юзер сам находит баги

## Предложение

Предустановленный Claude Code на каждом прод-сервере. После деплоя:
1. Story → `TESTING`
2. SSH на сервер → запуск Claude Code с промптом
3. Промпт содержит **только описание стори** (не таски — тестируем продуктовый вижн)
4. Claude тестирует как реальный пользователь (curl, Telethon для ботов)
5. Результат: pass → story = `completed` / fail → новая task в стори → engineering → deploy → testing (цикл)

## Архитектура

### Flow

```
Deploy success (smoke OK)
  → story = "testing"
  → publish QAMessage to qa:queue
      ↓
QA Consumer (services/langgraph/src/consumers/qa.py)
  → SSH to prod server
  → run Claude Code with QA prompt (timeout 20 min)
  → parse result (JSON: pass/fail + issues)
      ↓
  pass → story = "completed", notify user
  fail → POST /api/tasks/ (new task in same story)
       → story = "in_progress"
       → dispatcher picks up → engineering → deploy → testing
```

### Место в пайплайне

```
Engineering → CI → Deploy → Smoke → [QA] → Completed
                                      ↑        |
                                      |   fail: new task
                                      +--------+
```

### Новый статус: TESTING

```python
class StoryStatus(StrEnum):
    ...
    DEPLOYING = "deploying"
    TESTING = "testing"          # ← NEW
    WAITING_HUMAN_REVIEW = "waiting_human_review"
    COMPLETED = "completed"
    ...

VALID_TRANSITIONS = {
    ...
    StoryStatus.DEPLOYING: {TESTING, COMPLETED, IN_PROGRESS, FAILED},
    StoryStatus.TESTING: {COMPLETED, IN_PROGRESS, FAILED},  # ← NEW
    ...
}
```

API endpoint: `POST /api/stories/{id}/test` → transition to TESTING.

### Новая очередь: qa:queue

```python
# shared/queues.py
QA_QUEUE = "qa:queue"

# shared/contracts/queues/qa.py
class QAMessage(BaseMessage):
    story_id: str
    project_id: str
    user_id: str
    deployed_url: str
    server_ip: str
    story_description: str   # Полное описание стори (продуктовый вижн)
    bot_username: str | None = None  # Для Telegram-ботов
    qa_attempt: int = 0
```

### QA Consumer

Новый consumer в `services/langgraph/src/consumers/qa.py`. Не LangGraph-субграф — простой consumer как deploy, потому что основная логика делегирована Claude Code на сервере.

```python
async def process_qa_job(data: dict, redis: RedisStreamClient) -> dict:
    msg = QAMessage.model_validate(data)

    # 1. SSH to server, run Claude Code
    result = await _run_qa_on_server(
        server_ip=msg.server_ip,
        story_description=msg.story_description,
        deployed_url=msg.deployed_url,
        bot_username=msg.bot_username,
        timeout=1200,  # 20 min
    )

    # 2. Parse result
    if result["pass"]:
        await _transition_story_safe(msg.story_id, "complete")
        await publish_story_event(redis, user_id=msg.user_id,
            event="story_completed",
            text=f"QA passed. Project is live at {msg.deployed_url}")
    else:
        # Create fix task in same story
        await _create_qa_fix_task(msg, result["issues"])
        await _transition_story_safe(msg.story_id, "start")
        # dispatcher подхватит → engineering → deploy → qa (цикл)

    return {"status": "success" if result["pass"] else "qa_failed"}
```

### Изменения в deploy consumer

В `_handle_deploy_success()` — вместо сразу `complete`:

```python
# Было:
await _transition_story_safe(story_id, "complete")

# Стало:
await _transition_story_safe(story_id, "test")
await redis.publish_message(QA_QUEUE, QAMessage(
    story_id=story_id,
    project_id=project_id,
    user_id=user_id,
    deployed_url=result["deployed_url"],
    server_ip=server_ip,
    story_description=story_description,
    bot_username=bot_username,
))
```

Worker container НЕ удаляем до завершения QA — он может понадобиться для фикса.

### QA Prompt

Claude Code запускается на проде с промптом:

```
You are a QA tester. Test this deployed project as a real user would.

## Story (what the user asked for)
{story_description}

## Deployed at
- URL: {deployed_url}
- Bot: @{bot_username} (if applicable)

## Your task
1. Test every feature described in the story
2. For web/API: curl endpoints, check responses, test edge cases
3. For Telegram bots: use Telethon to send commands, check responses
4. Check that the UI/responses match the story description

## Rules
- Test ONLY what's described in the story — don't invent extra requirements
- Be practical: if the story says "show weather", check that weather is shown, not pixel-perfect
- Timeout: 20 minutes max

## Output
Return ONLY a JSON object:
{
  "pass": true/false,
  "checks": [
    {"name": "health endpoint", "pass": true, "detail": "GET /health → 200"},
    {"name": "weather endpoint", "pass": false, "detail": "GET /api/weather/moscow → 404, expected weather data"}
  ],
  "summary": "2/3 checks passed. Weather endpoint returns 404 instead of weather data."
}
```

### Запуск на сервере

```bash
ssh deploy@{server_ip} 'cd /opt/services/{project_name} && \
  timeout 1200 claude -p "{qa_prompt}" \
    --output-format json \
    --max-turns 50 \
    --model claude-sonnet-4-6 \
    --allowedTools "Bash(command:curl*)" "Bash(command:docker*)" "Read" \
    2>/dev/null'
```

Ключевые моменты:
- `--model claude-sonnet-4-6` — дешевле чем opus, достаточно для QA
- Все tools разрешены (упрощаем MVP, ограничим позже если нужно)
- `timeout 1200` — системный таймаут на 20 минут
- Рабочая директория — проект пользователя (доступ к docker-compose.yml, логам)
- `--output-format json` — структурированный результат

### Telethon для ботов

Для тестирования Telegram-ботов нужен Telethon с авторизованной сессией на проде.

**Setup (один раз)**:
1. Получаем Telegram API credentials (api_id, api_hash) с my.telegram.org
2. Логиним сессию интерактивно на одном из серверов
3. Сохраняем session file
4. Распространяем session file на все серверы через Ansible

**Хранение**:
- Session file в `/opt/qa-runner/telethon.session` на каждом сервере
- API credentials в env vars (`QA_TELEGRAM_API_ID`, `QA_TELEGRAM_API_HASH`)
- Всё provisioned через Ansible

**Использование в промпте**:
```
For Telegram bot testing, Telethon is pre-installed.
Session file: /opt/qa-runner/telethon.session
Use: python3 -c "from telethon.sync import TelegramClient; ..."
```

### Provisioning (Ansible)

Расширяем существующий `provision_software.yml` новой ролью:

```yaml
# services/infra-service/ansible/roles/qa_runner/tasks/main.yml
---
- name: Create QA runner directory
  file:
    path: /opt/qa-runner
    state: directory
    owner: "{{ deploy_user }}"
    mode: '0755'

- name: Install Node.js (for Claude Code)
  apt:
    name: [nodejs, npm]
    state: present

- name: Install Claude Code CLI
  npm:
    name: "@anthropic-ai/claude-code"
    global: true
    state: present

- name: Install Python packages for QA
  pip:
    name: [telethon, httpx, playwright]
    state: present

- name: Install Playwright browsers
  command: playwright install chromium
  become_user: "{{ deploy_user }}"

- name: Set Anthropic API key
  copy:
    content: "{{ anthropic_api_key }}"
    dest: /opt/qa-runner/.anthropic_key
    owner: "{{ deploy_user }}"
    mode: '0600'

- name: Copy Telethon session (if exists)
  copy:
    src: telethon.session
    dest: /opt/qa-runner/telethon.session
    owner: "{{ deploy_user }}"
    mode: '0600'
  when: telethon_session_exists | default(false)
```

**Важно**: Ansible с `state: present` — идемпотентный, не переустанавливает если уже есть. Можно запускать повторно — сделает только дифф.

### Retry и лимиты

| Параметр | Значение | Причина |
|----------|----------|---------|
| QA timeout | 20 мин | Достаточно для 10-15 проверок |
| Max QA attempts per story | 3 | Как у deploy retries |
| Max QA→Engineering loops | 2 | После 2 неудачных фиксов → story=failed, HITL |
| Model | sonnet | Дешевле, достаточно для тестирования |
| Allowed tools | all (MVP, ограничим позже) | Простота старта |

### Deduplication

Redis inflight marker (как у scaffold):
```python
QA_INFLIGHT_KEY = "qa:inflight"

async def _mark_qa_inflight(redis: RedisStreamClient, story_id: str) -> bool:
    key = f"{QA_INFLIGHT_KEY}:{story_id}"
    return bool(await redis.redis.set(key, "1", nx=True, ex=1500))  # 25 min TTL
```

## Что НЕ делаем в MVP

1. **Staging environment** — тестируем на проде, staging потом
2. **Playwright MCP** — начинаем с curl/httpx, браузерное тестирование потом
3. **Контейнер для QA** — голый Claude на проде, контейнеризация потом
4. **Visual regression** — только функциональные проверки
5. **Отдельный QA-субграф в LangGraph** — простой consumer достаточно
6. **Автогенерация acceptance criteria** — PO уже пишет описание стори, этого достаточно

## Компоненты для реализации

| # | Что | Где | Объём |
|---|-----|-----|-------|
| 1 | Статус `TESTING` + transitions | `shared/contracts/dto/story.py`, API router | ~20 строк |
| 2 | `QAMessage` контракт | `shared/contracts/queues/qa.py` | ~30 строк |
| 3 | `QA_QUEUE` constant | `shared/queues.py` | 3 строки |
| 4 | QA consumer | `services/langgraph/src/consumers/qa.py` | ~150 строк |
| 5 | Изменения в deploy consumer | `services/langgraph/src/consumers/deploy.py` | ~20 строк |
| 6 | Ansible role `qa_runner` | `services/infra-service/ansible/roles/qa_runner/` | ~50 строк |
| 7 | Docker entrypoint для qa-worker | `docker-compose.yml` | ~15 строк |
| 8 | API endpoint `POST /stories/{id}/test` | `services/api/src/routers/stories.py` | ~15 строк |

## Открытые вопросы

### 1. Как получить story description в deploy consumer?
Сейчас `DeployMessage` не содержит описание стори — только `story_id`. Нужно либо:
- (A) Fetch из API в deploy consumer: `GET /api/stories/{story_id}` → description
- (B) Fetch в QA consumer (лучше — deploy consumer не трогаем лишний раз)

**Рекомендация**: (B) — QA consumer сам достаёт описание по story_id.

### 2. Как получить server_ip?
Deploy consumer знает server через `DevOpsState`, но `_handle_deploy_success()` получает только `result` dict. Нужно пробросить `server_ip` или достать из API по project_id.

**Рекомендация**: QA consumer достаёт server из API: `GET /api/projects/{project_id}` → server → ip.

### 3. bot_username для Telegram-ботов?
Нужно знать username бота для Telethon тестирования. Хранится в secrets проекта (`TELEGRAM_BOT_TOKEN`). Можно получить через Bot API: `getMe` → username.

**Рекомендация**: QA consumer достаёт bot token из project secrets, вызывает `getMe`.

### 4. Claude Code auth на серверах?
Варианты:
- (A) API key в файле — простой, но нужно безопасно доставить
- (B) Env var `ANTHROPIC_API_KEY` — стандартный способ

**Рекомендация**: (B) — Ansible кладёт в `/opt/qa-runner/.env`, Claude Code подхватывает.

## Action Items

- → new task: "Add TESTING status to StoryStatus + API transition endpoint" — shared/contracts + stories router
- → new task: "QA queue contract + consumer skeleton" — QAMessage, qa:queue, basic consumer that SSH-es to server and runs Claude Code
- → new task: "Wire deploy consumer to qa:queue" — replace story=completed with story=testing + publish QAMessage after deploy success
- → new task: "Ansible role: qa_runner provisioning" — install Claude Code, Telethon, Playwright on prod servers (idempotent, diff only)
- → new task: "QA failure → create task + story loop" — on QA fail, create fix task in same story, transition to in_progress
- → idea: "Telethon session management — login flow + session distribution across servers"
- → idea: "QA budget tracking — token usage per QA run, cost alerts"
- → idea: "Acceptance criteria extraction — PO generates testable criteria in structured format"

