# Внешние Coding Agents

Для задач разработки используем production-ready инструменты вместо написания своих агентов.

## Текущая реализация: Factory.ai Droid

Автономный coding agent с уровнями автономности.

```bash
# Интерактивный режим
droid

# Single-shot (для автоматизации)
droid exec "Implement feature X" --autonomy high

# Из файла (используется в coding-worker)
droid exec --prompt-file TASK.md --skip-permissions-unsafe
```

**Autonomy levels:** low (много подтверждений), medium, high (полная автономия).

## Claude Code

Claude Code — CLI-инструмент от Anthropic для agentic coding. Реализован наравне с Droid.

```bash
# Установка (native installer)
curl -fsSL https://claude.ai/install.sh | sh

# Использование
claude -p "Implement user registration endpoint"

# Pipe
cat error.log | claude -p "Fix this error"
```

**Контекст:** Нативно использует `CLAUDE.md` файлы. Worker-manager автоматически маппит `INSTRUCTIONS.md` → `CLAUDE.md`.

**Цена:** Pro/Max подписка (~$20-100/мес), дешевле чем API.

---

## Интеграция в проект

Developer node в Engineering Subgraph использует coding agents через `worker-manager` сервис (PO не использует контейнеры — это LangGraph ReactAgent):

1. Worker-manager создаёт контейнер из worker-base образа
2. Монтирует pre-scaffolded workspace (`/data/workspaces/{repo_id}/`) — код уже на месте
3. Worker-manager creates/checks out story feature branch (`story/{story_id}`)
4. Инжектит статические инструкции из `services/langgraph/src/prompts/developer_worker/INSTRUCTIONS.md` → agent-specific file (`CLAUDE.md` / `AGENTS.md`)
5. Инжектит динамический `TASK.md` в `/workspace/TASK.md` с project-specific задачей. Previous tasks archived in `.story/old_tasks/`
6. Запускает coding agent (Droid или Claude Code) с одной строкой: `claude -p "Read TASK.md"`
7. Агент коммитит и пушит на feature branch. Worker-wrapper pulls from current branch (not hardcoded `main`)
8. Агент сообщает результат через HTTP: `curl -X POST localhost:9090/result -d '{"success":true,"commit":"<sha>","summary":"..."}'`
9. При невозможности выполнения: `curl -X POST localhost:9090/result -d '{"success":false,"reason":"..."}'`

**Worker-wrapper HTTP сервер** (`localhost:9090`):
- `POST /result` — единый endpoint для результатов (success/failure). Auto-resume: если агент завершается без вызова `/result`, wrapper автоматически перезапускает его один раз.
- `POST /infra/compose` — compose proxy для управления sidecar-инфраструктурой (db, redis). Проксируется в worker-manager.
- Makefile override targets (`make migrate`, `make dev-start`) внутри воркера используют `curl localhost:9090/infra/compose`.

---

## Маппинг на узлы графа

| Узел | Инструмент | Статус |
|------|------------|--------|
| **Scaffolder** | Copier template | ✅ Реализовано |
| **Developer** | Factory.ai Droid / Claude Code | ✅ Реализовано (Native execution, Flat Dev Environment) |
| **Tester** | Функциональный узел (запуск тестов) | ⚠️ Заглушка (временно Developer сам запускает тесты через `make`) |
| **DevOps** | GitHub Actions (deploy.yml) | ✅ Реализовано |
