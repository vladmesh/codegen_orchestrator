# AGENTS — Frontend

## Overview

Node.js frontend сервис. Запускается в отдельном Docker-контейнере.

- Весь код в `services/frontend`. Не выносите файлы за пределы.
- Запуск через `make dev-start` (профиль `frontend` в docker compose).
- Точка входа и фреймворк определяются в `package.json` и `src/`.

## Key Commands

| Команда | Назначение |
|---------|-----------|
| `make dev-start` | Запустить dev-окружение (включает frontend) |
| `make tests frontend` | Тесты frontend (если настроены) |
| `make log frontend` | Логи контейнера |

## Environment Variables

Все переменные окружения определяются в `.env.example`. Не используйте значения по умолчанию.

## Notes

- Этот сервис является placeholder'ом. После настройки фреймворка (React, Vue, Astro и т.д.) обновите этот файл.
- Compose-конфигурация: `infra/compose.base.yml` и `infra/compose.dev.yml`.
