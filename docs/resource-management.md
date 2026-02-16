# Resource Management & Secrets Isolation

> Ресурсы выделяются через `ResourceAllocator` в Engineering Worker. Этот документ описывает паттерн изоляции секретов, используемый в системе.

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
│  project.config.secrets (PostgreSQL, Fernet-encrypted)     │
│                                                             │
│  telegram_bots:                                            │
│    handle_abc123:                                          │
│      name: "@weather_bot"                                  │
│      token: "gAAAAA..."  ← зашифрован Fernet at rest      │
└─────────────────────────────────────────────────────────────┘
```

## Текущая реализация: PostgreSQL + Fernet encryption

Секреты хранятся в поле `config.secrets` модели `Project`, зашифрованные Fernet at rest:

```python
from shared.crypto import decrypt_dict, encrypt_dict

# Чтение: decrypt после получения из API
config_secrets = project_spec.get("config", {}).get("secrets", {})
config_secrets = decrypt_dict(config_secrets) if config_secrets else {}

# Запись: encrypt перед отправкой в API
config["secrets"] = encrypt_dict(secrets)
await api_client.patch(f"/projects/{project_id}", json={"config": config})
```

Ключ шифрования: env var `SECRETS_ENCRYPTION_KEY` (Fernet key). При отсутствии — `RuntimeError` при первом вызове encrypt/decrypt.

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

- [SECRETS.md](SECRETS.md) — архитектура управления секретами (уровни L1/L2/L3)
- [secrets-vault-implementation.md](tasks/secrets-vault-implementation.md) — исторический план (superseded by Fernet encryption in `shared/crypto.py`)
