# Resource Management (Завхоз)

Завхоз — узел LangGraph, управляющий ресурсами с изоляцией секретов от LLM.

## Принцип: LLM никогда не видит секреты

```
┌─────────────────────────────────────────────────────────────┐
│                    LangGraph State                          │
│  (это видит LLM)                                           │
│                                                             │
│  allocated_resources: {                                    │
│      "telegram_bot": "handle_abc123",  ← handle, не токен  │
│      "server": "prod_vps_1"            ← имя, не IP/SSH    │
│  }                                                          │
└─────────────────────────────────────────────────────────────┘
                           │
                           ▼
┌─────────────────────────────────────────────────────────────┐
│                  Завхоз (узел LangGraph)                    │
│                                                             │
│  LLM-часть:                                                │
│  - Решает КАКОЙ ресурс нужен                               │
│  - Возвращает handle/имя в state                           │
│                                                             │
│  Python-часть (вне видимости LLM):                         │
│  - Читает реальные секреты из storage                      │
│  - Передаёт в subprocess через env vars                    │
└─────────────────────────────────────────────────────────────┘
                           │
                           ▼
┌─────────────────────────────────────────────────────────────┐
│                   Secrets Storage                           │
│  project.config.secrets (PostgreSQL)                       │
│                                                             │
│  telegram_bots:                                            │
│    handle_abc123:                                          │
│      name: "@weather_bot"                                  │
│      token: "123456:ABC..."  ← реальный токен             │
└─────────────────────────────────────────────────────────────┘
```

## Текущая реализация: PostgreSQL

Секреты хранятся в поле `config.secrets` модели `Project` и управляются через API:

```python
# Сохранение секрета через API
await api_client.save_project_secret(project_id, "TELEGRAM_TOKEN", "123456:ABC...")

# DevOps subgraph читает секреты из project_spec.config.secrets
secrets = project_spec.get("config", {}).get("secrets", {})
```

## Типы секретов

DevOps subgraph классифицирует переменные окружения на три типа:

| Тип | Описание | Пример |
|-----|----------|--------|
| `infra` | Генерируются автоматически | `DATABASE_URL`, `REDIS_URL` |
| `computed` | Вычисляются из контекста | `APP_NAME`, `PORT` |
| `user` | Требуются от пользователя | `TELEGRAM_BOT_TOKEN`, `API_KEY` |

## Что хранит Завхоз

| Категория | Handle пример | Реальные данные |
|-----------|---------------|-----------------|
| Telegram боты | `handle_abc123` | token, username |
| Серверы | `prod_vps_1` | IP, SSH key |
| API ключи | `openai_main` | API key |
| Домены | `example.com` | Cloudflare credentials |

## Управление Инфраструктурой (Server Management)

Система поддерживает гибридную инфраструктуру, синхронизируемую с провайдером (Time4VPS).

1.  **Source of Truth**: База данных (`api` сервис).
    *   Фоновый worker (`server_sync.py`) каждую минуту опрашивает Time4VPS API.
    *   Новые сервера автоматически добавляются со статусом `discovered`.
    *   Удаленные сервера помечаются как `missing`.

2.  **Ghost Servers & Filtering**:
    *   Сервера, которые нужно игнорировать (личные машины разработчиков), прописываются в `GHOST_SERVERS`.
    *   В базе они помечаются как `is_managed=False`.
    *   Zavhoz использует инструмент `list_managed_servers`, который возвращает только `is_managed=True`.

## GitHub App & Secrets

Для работы с GitHub (создание репозиториев, управление workflows) используется GitHub App.

| Secret Name | Описание | Где хранится |
|-------------|----------|--------------|
| `GH_APP_ID` | App ID приложения Project-Factory-Keeper | GitHub Secrets |
| `GH_APP_PRIVATE_KEY` | Private Key (.pem) для подписи JWT | GitHub Secrets |

**Локальная разработка:**
- `GITHUB_APP_ID` → `.env`
- Private Key → `~/.gemini/keys/github_app.pem` (mount в docker-compose)

**Production:**
- Secrets записываются на сервер через CI/CD workflow
- Путь на проде: `/opt/secrets/github_app.pem`

## См. также

- [secrets-vault-implementation.md](tasks/secrets-vault-implementation.md) — детальный план реализации хранилища секретов
