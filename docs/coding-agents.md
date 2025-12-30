# Внешние Coding Agents

Для задач разработки используем production-ready инструменты вместо написания своих агентов.

## Claude Code (Anthropic)

CLI-инструмент для agentic coding. Понимает весь codebase, редактирует файлы, запускает команды.

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

## Factory.ai Droid

Автономный coding agent с уровнями автономности.

```bash
# Интерактивный режим
droid

# Single-shot (для автоматизации)
droid exec "Implement feature X" --autonomy high

# Из файла
droid exec --prompt-file task.md
```

**Autonomy levels:** low (много подтверждений), medium, high (полная автономия).

## Маппинг на узлы графа

| Узел | Инструмент | Почему |
|------|------------|--------|
| **Архитектор** | Claude Code | Понимает codebase, генерит структуру |
| **Разработчик** | Droid (high autonomy) | Автономная реализация фич |
| **Тестировщик** | Claude Code / Droid | Пишут и запускают тесты |
| **DevOps** | Custom (Ansible wrapper) | Специфичная задача |
| **Завхоз** | LangGraph native | Доступ к секретам |

## Интеграция в LangGraph

```python
import subprocess

async def developer_node(state: dict) -> dict:
    """Developer node using external coding agent."""
    task = state["current_task"]
    project_path = state["project_path"]
    
    # Записываем контекст для агента
    Path(f"{project_path}/TASK.md").write_text(task["description"])
    
    # Вызываем Claude Code
    result = subprocess.run(
        ["claude", "-p", "Read TASK.md and implement. Run tests."],
        cwd=project_path,
        capture_output=True,
        text=True
    )
    
    return {
        "messages": [AIMessage(content=result.stdout)],
        "current_agent": "developer"
    }
```
