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

Developer node в Engineering Subgraph использует Droid через `coding-worker` контейнер:

1. Worker Spawner создаёт контейнер `coding-worker:latest`
2. Контейнер клонирует репозиторий
3. Записывает `TASK.md` и `AGENTS.md` с инструкциями
4. Запускает `droid exec --skip-permissions-unsafe`
5. Коммитит и пушит изменения

```python
# services/coding-worker/scripts/execute_task.sh
droid exec --prompt-file TASK.md --skip-permissions-unsafe
git add -A && git commit -m "feat: implement task" && git push
```

---

## 🚧 Планируется: Claude Code

> [!NOTE]
> Следующий раздел описывает **запланированную**, но ещё не реализованную функциональность.

Claude Code — CLI-инструмент от Anthropic для agentic coding. Рассматривается как альтернатива/дополнение к Droid.

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
