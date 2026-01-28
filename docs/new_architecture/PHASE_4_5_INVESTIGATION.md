# Phase 4.5 — Worker Mock Anthropic Integration: Investigation Report

> **Date**: 2026-01-18  
> **Status**: ✅ Resolved  
> **Related**: [E2E_ENGINEERING_TEST_PLAN.md](./E2E_ENGINEERING_TEST_PLAN.md)

---

## 1. Цель теста

Создать промежуточный тест, который проверяет что:

1. Worker-контейнер успешно создаётся через Redis команду
2. Claude CLI внутри контейнера отправляет запросы на mock-anthropic сервер
3. Mock-сервер возвращает детерминированные ответы
4. Worker-wrapper получает ответ и публикует его в Redis stream

```
┌─────────────────┐     ┌─────────────────┐     ┌─────────────────┐
│   Test Runner   │────▶│  Worker (DIND)  │────▶│  Mock Anthropic │
│                 │     │                 │     │  172.30.0.40    │
│ Redis commands  │     │  Claude CLI     │     │  /v1/messages   │
└─────────────────┘     └─────────────────┘     └─────────────────┘
        │                       │
        │◀──────────────────────│
        │   worker:{id}:output  │
```

---

## 2. Корневая причина проблемы

### 2.1 Симптомы

Worker-контейнеры создавались успешно, но не производили output:
- Контейнер стартует ✅
- Сеть подключена ✅
- HTTP connectivity до mock-anthropic работает ✅
- Но `worker:{id}:output` stream остаётся пустым ❌

### 2.2 Причина

**Deprecated npm пакет `@anthropic-ai/claude-code`.**

Мы использовали устаревший способ установки:
```dockerfile
# DEPRECATED - НЕ РАБОТАЕТ КОРРЕКТНО
RUN npm install -g @anthropic-ai/claude-code
```

Этот пакет имел проблемы с:
- Поддержкой `ANTHROPIC_API_KEY` environment variable
- Работой с `ANTHROPIC_BASE_URL` для кастомного endpoint

### 2.3 Документация Anthropic

Из официальной документации (январь 2026):

> **NPM installation (deprecated)**  
> We recommend using the native installation instead.

Рекомендуемый способ:
```bash
curl -fsSL https://claude.ai/install.sh | bash
```

---

## 3. Решение

### 3.1 Обновлён `worker-base-claude/Dockerfile`

**Было:**
```dockerfile
RUN npm install -g @anthropic-ai/claude-code
```

**Стало:**
```dockerfile
# Install Claude Code via native installer (npm version is deprecated)
RUN curl -fsSL https://claude.ai/install.sh | bash

# Add Claude launcher to PATH
ENV PATH="/home/worker/.local/bin:${PATH}"

# Create minimal config for API key mode
RUN mkdir -p /home/worker/.claude && \
    echo '{}' > /home/worker/.claude/settings.json

# Disable auto-updates and telemetry in containerized environment
ENV DISABLE_AUTOUPDATER=1
ENV DISABLE_TELEMETRY=1
```

### 3.2 Очищен `universal-worker/Dockerfile`

Удалены Claude-специфичные зависимости:
- ❌ `nodejs`, `npm`
- ❌ `ralph-wiggum` plugin installation

Теперь universal-worker — действительно универсальный базовый образ для любого агента.

### 3.3 Проверка

```bash
$ docker run --rm --entrypoint /bin/bash worker-base-claude:latest -c 'claude --version'
2.1.12 (Claude Code)
```

---

## 4. Как работает Claude CLI с API Key

После перехода на native installer, Claude CLI корректно поддерживает:

```bash
export ANTHROPIC_API_KEY="sk-ant-api-..."
export ANTHROPIC_BASE_URL="http://mock-server:8000"
claude -p "test" --output-format json
```

Ключевые моменты:
1. `ANTHROPIC_API_KEY` имеет **приоритет** над OAuth сессией
2. `ANTHROPIC_BASE_URL` позволяет направить запросы на mock-сервер
3. Создание `~/.claude/settings.json` (даже пустого `{}`) предотвращает ошибки при первом запуске

---

## 5. Дополнительные проблемы и решения

После миграции на native installer были обнаружены дополнительные проблемы:

### 5.1 ResultParser не понимал Claude CLI JSON формат

**Симптом**: Worker контейнер получал ответ от mock-anthropic, но `worker:{id}:output` оставался пустым.

**Причина**: Claude CLI с `--output-format json` возвращает:
```json
{"type": "result", "result": "...<result>JSON</result>...", "session_id": "..."}
```

ResultParser искал `<result>` теги в сыром JSON, а не в поле `result`.

**Решение**: Добавлен метод `_extract_result_text()`:
```python
@classmethod
def _extract_result_text(cls, stdout: str) -> str:
    try:
        data = json.loads(stdout)
        if isinstance(data, dict) and "result" in data:
            return data["result"]
    except (json.JSONDecodeError, TypeError):
        pass
    return stdout
```

### 5.2 Mock server извлекал только первый text block

**Симптом**: Тест `test_worker_response_matches_scenario` получал "All tests passed" вместо "Implementation completed successfully".

**Причина**: Claude CLI отправляет сообщения с несколькими text blocks:
```json
{"role": "user", "content": [
  {"type": "text", "text": "<system-reminder>CLAUDE.md context...</system-reminder>"},
  {"type": "text", "text": "Please implement the feature."}
]}
```

Mock server извлекал только первый блок (контекст CLAUDE.md), который содержал слово "test" из `make test-unit`, игнорируя реальный промпт пользователя.

**Решение**: Конкатенация ВСЕХ text blocks:
```python
# Handle content blocks - concatenate ALL text blocks
text_parts = []
for block in content:
    if isinstance(block, dict) and block.get("type") == "text":
        text_parts.append(block.get("text", ""))
last_user_message = "\n".join(text_parts)
```

---

## 6. Результаты тестов

```bash
$ make test-e2e-worker-mock

tests/e2e/test_worker_mock_anthropic.py::test_worker_receives_mock_response PASSED
tests/e2e/test_worker_mock_anthropic.py::test_worker_response_matches_scenario PASSED

==================== 2 passed in 394.85s ====================
```

---

## 7. Файлы изменены

| Файл | Изменение |
|------|-----------|
| `services/worker-manager/images/worker-base-claude/Dockerfile` | Native installer вместо npm |
| `services/universal-worker/Dockerfile` | Удалены npm, nodejs, plugins |
| `packages/worker-wrapper/src/worker_wrapper/result_parser.py` | Поддержка Claude CLI JSON формата |
| `packages/worker-wrapper/tests/unit/test_result_parser.py` | Тесты для Claude CLI JSON |
| `tests/e2e/mock_anthropic/server.py` | Извлечение всех text blocks |
| `tests/e2e/test_worker_mock_anthropic.py` | Исправлены assertions |
| `docs/new_architecture/E2E_ENGINEERING_TEST_PLAN.md` | Обновлён статус Phase 4.5 |

---

## Приложение: Структура ~/.claude/

```
~/.claude/
├── settings.json          # Пользовательские настройки (создаём пустой {})
├── debug/                 # Debug логи
├── downloads/             # Скачанные версии
├── statsig/               # Feature flags
└── todos/                 # Todos
```

Бинарник устанавливается в:
```
~/.local/share/claude/versions/X.X.X
~/.local/bin/claude -> ~/.local/share/claude/versions/X.X.X  (symlink)
```
