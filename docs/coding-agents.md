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
2. Контейнер клонирует репозиторий
3. Инжектит статические инструкции из `services/langgraph/src/prompts/developer_worker/INSTRUCTIONS.md` → agent-specific file (`CLAUDE.md` / `AGENTS.md`)
4. Инжектит динамический `TASK.md` с project-specific задачей
5. Запускает coding agent (Droid или Claude Code)
6. Агент коммитит и пушит изменения

---

## Маппинг на узлы графа

| Узел | Инструмент | Статус |
|------|------------|--------|
| **Scaffolder** | Copier template | ✅ Реализовано |
| **Developer** | Factory.ai Droid / Claude Code | ✅ Реализовано |
| **Tester** | Функциональный узел (запуск тестов) | ⚠️ Заглушка |
| **DevOps** | Ansible wrapper | ✅ Реализовано |
