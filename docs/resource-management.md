# Resource Management & Secrets Isolation

> Ресурсы выделяются через `ResourceAllocator` в Engineering Worker. Этот документ описывает паттерн изоляции секретов, используемый в системе.

## Принцип: LLM никогда не видит секреты

```
┌─────────────────────────────────────────────────────────────┐
│                    LangGraph State                          │
│  (это видят агенты - Product Owner)                        │
│                                                             │
│  allocated_resources: {                                    │
│      "server_handle:8000": {                               │
│          "port": 8000,                                     │
│          "server_handle": "prod_vps_1",  ← имя, не IP/SSH  │
│          "service_name": "backend"                         │
│      }                                                     │
│  }                                                          │
└─────────────────────────────────────────────────────────────┘
                           │
                           ▼
┌─────────────────────────────────────────────────────────────┐
│                  Выделение ресурсов                         │
│                                                             │
│  Functional-часть (ResourceAllocatorNode в Engineering):   │
│  - Автоматически выделяет порты и сервера через API        │
│  - Переиспользует логику из `tools/allocator.py`           │
│  - НЕ использует LLM (полностью детерминировано)           │
└─────────────────────────────────────────────────────────────┘
                           │
                           ▼
┌─────────────────────────────────────────────────────────────┐
│                   Secrets Storage                           │
│  project.config.secrets (PostgreSQL, Fernet-encrypted)     │
│                                                             │
│  Пример (телеграм токен):                                  │
│  В БД: "gAAAAA..."  ← зашифрован Fernet at rest            │
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

## Взаимодействие Product Owner'а с секретами

Агент Product Owner напрямую запрашивает у пользователя секреты (например, `TELEGRAM_BOT_TOKEN`), если они требуются для выбранных модулей.
PO вызывает tool `set_project_secret`, который сохраняет токен в БД, сразу шифруя его через Fernet. Никакие инфраструктурные ключи (SSH, БД) PO не видит и не генерирует.

## Управление Инфраструктурой (Server Management)

Система поддерживает гибридную инфраструктуру, синхронизируемую с провайдером (Time4VPS).

1.  **Source of Truth**: База данных (`api` сервис).
    *   Фоновый worker (`server_sync.py`) каждую минуту опрашивает Time4VPS API.
    *   Новые сервера автоматически добавляются со статусом `discovered`.
    *   Удаленные сервера помечаются как `missing`.

2.  **Ghost Servers & Filtering**:
    *   Сервера, которые нужно игнорировать (личные машины разработчиков), прописываются в `GHOST_SERVERS`.
    *   В базе они помечаются как `is_managed=False`.
    *   `ResourceAllocator` использует функцию `list_managed_servers`, которая возвращает только `is_managed=True`.

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

## Worker Garbage Collection (Управление мусором)

Для параллельных воркеров (см. [docs/parallel-workers.md](parallel-workers.md)) система создает временные ресурсы (workspaces, networks, containers) на хосте.

1. **Жизненный цикл**:
   * Воркер получает workspace директорию (pre-scaffolded: `/data/workspaces/{repo_id}/`, ephemeral: `/tmp/codegen/workspaces/{worker_id}/`) и изолированную Docker сеть `dev_proj_<worker_id>`.
   * Агент вызывает `orchestrator dev-env compose up`/`down` для управления sidecar'ами внутри этого пространства имён.
2. **Очистка (Garbage Collection)**:
   * Явное удаление: при завершении LangGraph вызывает `worker-manager` `delete_worker`, который удаляет контейнеры, сеть, и пространство на диске.
   * Фоновый сбор мусора (GC): `scheduler` раз в 30 минут триггерит GC в `worker-manager`. Метод `WorkerManager.garbage_collect_orphaned_resources()` находит "осиротевшие" контейнеры воркеров, сети `dev_proj_*` и директории на диске (сопоставляя с активными ключами `worker:status:*` в Redis) и удаляет их, защищая систему от утечек после крэшей или OOM-событий.

## См. также

- [SECRETS.md](SECRETS.md) — архитектура управления секретами (уровни L1/L2/L3)
- [secrets-vault-implementation.md](tasks/secrets-vault-implementation.md) — исторический план (superseded by Fernet encryption in `shared/crypto.py`)
