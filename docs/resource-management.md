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
│                   Encrypted Storage                         │
│  (SOPS + YAML, позже PostgreSQL)                           │
│                                                             │
│  telegram_bots:                                            │
│    handle_abc123:                                          │
│      name: "@weather_bot"                                  │
│      token: "123456:ABC..."  ← реальный токен             │
└─────────────────────────────────────────────────────────────┘
```

## Пример: деплой использует секреты, но LLM их не видит

```python
@tool
def deploy_to_server(server_handle: str, project_path: str):
    """Deploy project to server. LLM calls this with handle only."""
    # Python-код читает секреты напрямую, минуя LLM
    server = secret_storage.get_server(server_handle)
    
    subprocess.run(
        ["ansible-playbook", "playbooks/site.yml"],
        env={
            "ANSIBLE_HOST": server.host,        # LLM не видит
            "ANSIBLE_SSH_KEY": server.ssh_key,  # LLM не видит
        }
    )
    return "Deployed successfully"  # ← только это в контекст
```

## Хранение секретов (MVP)

SOPS + AGE для шифрования YAML файла:

```yaml
# secrets.yaml (зашифрован SOPS)
telegram_bots:
    handle_abc123:
        name: "@weather_bot"
        token: ENC[AES256_GCM,data:...,iv:...,tag:...]

servers:
    prod_vps_1:
        host: ENC[AES256_GCM,data:...,iv:...,tag:...]
        ssh_key: ENC[AES256_GCM,data:...,iv:...,tag:...]

api_keys:
    openai:
        key: ENC[AES256_GCM,data:...,iv:...,tag:...]
```

```bash
# Расшифровка при старте оркестратора
export SOPS_AGE_KEY_FILE=~/.age/key.txt
sops -d secrets.yaml > /tmp/secrets.yaml
```

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

- [secrets-vault-design.md](secrets-vault-design.md) — детальный дизайн хранилища секретов
