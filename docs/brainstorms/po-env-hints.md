# Brainstorm: PO Env Hints (Context-Aware Environment Variables)

**Status:** DONE
**Date:** 2026-03-06

## Проблема

Сейчас PO ReactAgent работает с пользователем, но передает Девелоперу только сырой текстовый `description`. Если пользователь дает свой API-ключ или просит "сделать бота только для меня", PO не может элегантно передать эти данные Девелоперу в виде готовых переменных окружения с пояснениями. В результате Девелопер либо хардкодит ключи в коде, либо придумывает свои имена переменных, которые пользователь потом должен вводить заново при деплое.

ПО должен уметь сам сохранять переменные и передавать Девелоперу подсказки (hints) о том, какие переменные уже заданы и как их использовать.

## Предлагаемое решение

Разрешить PO управлять конфигом проекта на этапе формирования ТЗ и передавать Девелоперу список `env_hints`.

### 1. PO Context Injection
Прокидывать `user_id` и `user_name` в контекст PO Агента (через SystemMessage в начале треда), чтобы PO знал, с кем он говорит, и мог использовать этот ID для ограничения доступа.

### 2. Поддержка подсказок (Hints) для секретов
Расширить `ProjectDTO.config` полем `env_hints: dict[str, str]`.
Обновить инструмент `set_project_secret(project_id, key, value, hint="")` у PO:
- Значение (`value`) шифруется и ложится в `secrets`.
- Подсказка (`hint`) сохраняется в открытом виде в `config.env_hints`.

Обучить PO через `prompts.py` использовать этот механизм каждый раз, когда юзер дает чувствительные данные или просит ограничить доступ.

### 3. Инжект подсказок в промпт Девелопера
В Developer Subgraph (перед вызовом агента-программиста) читать `env_hints` из БД.
Если они есть, приклеивать к системному промпту программиста блок:
```markdown
## Provided Environment Variables
The Product Owner has already defined the following environment variables for this project. 
You MUST use them in your code via `os.getenv()` or `pydantic-settings`. Do NOT ask the user for them.

- ADMIN_TELEGRAM_ID: ID пользователя, которому бот должен отвечать на маты
- OPENAI_API_KEY: Ключ для генерации ответов
```

## Action Points (Экшнпоинты)

- [ ] В `services/langgraph/src/po/consumer.py` (`_handle_message`) прокидывать контекст пользователя (ID, name) в первые сообщения треда.
- [ ] В `services/langgraph/src/po/tools.py` изменить `set_project_secret`, добавив параметр `hint`, и сохранять его в `project_spec["config"]["env_hints"]`.
- [ ] В `services/langgraph/src/po/prompts.py` добавить инструкции для PO по использованию `hint` при сохранении `set_project_secret`.
- [ ] В `services/langgraph/src/subgraphs/developer/prompts.py` (или в соответствующей ноде, где формируется промпт) доставать `env_hints` из `project_spec` и вклеивать в промпт.
