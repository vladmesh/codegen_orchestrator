# Внешние Coding Agents

Для задач разработки используем production-ready инструменты вместо написания своих агентов.

## Текущая реализация: Factory.ai Droid

Автономный coding agent с уровнями автономности. **Это единственный coding agent, используемый в проекте.**

```bash
# Интерактивный режим
droid

# Single-shot (для автоматизации)
droid exec "Implement feature X" --autonomy high

# Из файла (используется в coding-worker)
droid exec --prompt-file TASK.md --skip-permissions-unsafe
```

**Autonomy levels:** low (много подтверждений), medium, high (полная автономия).

### Интеграция в проект

Developer node в Engineering Subgraph использует coding agents через `workers-spawner` сервис:

1. Workers-spawner создаёт контейнер из `universal-worker:latest`
2. Контейнер клонирует репозиторий
3. Записывает `TASK.md` и `AGENTS.md` с инструкциями
4. Запускает coding agent (Droid или Claude Code)
5. Коммитит и пушит изменения

---

## Claude Code

Claude Code — CLI-инструмент от Anthropic для agentic coding. Реализован наравне с Droid.

```bash
# Установка
npm install -g @anthropic-ai/claude-code

# Использование
claude -p "Implement user registration endpoint"

# Pipe
cat error.log | claude -p "Fix this error"
```

**Контекст:** Использует `CLAUDE.md` файлы (аналог нашего `AGENTS.md`).

**Цена:** Pro/Max подписка (~$20-100/мес), дешевле чем API.

---

## Маппинг на узлы графа

| Узел | Инструмент | Статус |
|------|------------|--------|
| **Architect** | LLM (GPT-4/Claude) + Preparer | ✅ Реализовано |
| **Preparer** | Copier template | ✅ Реализовано |
| **Developer** | Factory.ai Droid | ✅ Реализовано |
| **Tester** | Функциональный узел (запуск тестов) | ✅ Реализовано |
| **DevOps** | Ansible wrapper | ✅ Реализовано |
| **Zavhoz** | LangGraph native (LLM + tools) | ✅ Реализовано |
