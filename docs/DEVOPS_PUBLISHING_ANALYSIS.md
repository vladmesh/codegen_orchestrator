# DevOps & Publishing Pipeline Analysis

> Полный анализ того, что нужно доработать для работы публикации проектов и CI/CD.

## Executive Summary

Для MVP публикации требуется скоординированная доработка в трёх местах:
1. **codegen_orchestrator** - DevOps нода и Ansible плейбуки
2. **service_template** - CI workflow и compose конфигурация
3. **GitHub API** - настройка секретов репозитория

**Критический путь:**
```
Engineering завершён → Zavhoz выделил сервер:порт → DevOps деплоит проект →
GitHub Actions настроен → При пуше в main образ обновляется на сервере
```

---

## 1. Текущее состояние

### 1.1 Что существует

| Компонент | Статус | Файл |
|-----------|--------|------|
| DevOps нода | Реализована, но не протестирована | `services/langgraph/src/nodes/devops.py` |
| Zavhoz нода | Работает | `services/langgraph/src/nodes/zavhoz.py` |
| Server tools | Работают | `services/langgraph/src/tools/servers.py` |
| Port allocation | Работает | `services/langgraph/src/tools/ports.py` |
| deploy_project.yml | Устаревший формат | `services/infrastructure/ansible/playbooks/deploy_project.yml` |
| Routing engineering→devops | Работает | `services/langgraph/src/graph.py:224-247` |

### 1.2 Flow после Developer

```
developer_spawn_worker → tester → route_after_engineering
                                        ↓
                            engineering_status == "done" AND
                            allocated_resources != {} ?
                                        ↓
                                    devops → END
```

**Проблема:** Zavhoz и Engineering работают параллельно. К моменту завершения Engineering, Zavhoz должен был успеть выделить ресурсы. Если нет — проект не публикуется.

---

## 2. Проблемы DevOps ноды

### 2.1 Ansible плейбуки не в Docker образе

**Файл:** `services/langgraph/Dockerfile`

DevOps нода ожидает плейбуки по пути:
```python
playbook_path = "/app/services/infrastructure/ansible/playbooks/deploy_project.yml"
```

Но Dockerfile не копирует папку `infrastructure`:
```dockerfile
# Текущее состояние - infrastructure НЕ копируется
COPY services/langgraph/src ./src
```

**Решение:**
```dockerfile
# Добавить копирование Ansible
COPY services/infrastructure ./services/infrastructure
```

### 2.2 SSH ключ не доступен в контейнере

**Файл:** `services/langgraph/src/nodes/devops.py:137-141`

```python
inventory_content = (
    f"{target_server_ip} ansible_user=root "
    "ansible_ssh_private_key_file=/root/.ssh/id_ed25519 "  # <-- Ключ не монтируется!
    "ansible_ssh_common_args='-o StrictHostKeyChecking=no'"
)
```

**Решение:** Передавать SSH ключ через secrets volume или environment:
```yaml
# docker-compose.yml
services:
  langgraph:
    volumes:
      - /opt/secrets/ssh:/root/.ssh:ro
```

Или использовать ключ из базы (`Server.ssh_key_enc`), расшифровывая на лету.

### 2.3 Устаревший формат deploy_project.yml

**Текущий плейбук ожидает:**
```yaml
- name: Download docker-compose file
  get_url:
    url: "https://...github.com/.../main/docker-compose-prod.yml"  # НЕ СУЩЕСТВУЕТ!
```

**service_template генерирует:**
```
infra/
  compose.base.yml
  compose.prod.yml
  .env.prod
```

**Решение:** Переписать плейбук под новый формат (см. секцию 4).

### 2.4 Недостаточная конфигурация .env

**Текущий плейбук создаёт:**
```yaml
- name: Create .env file
  copy:
    content: |
      PORT={{ service_port }}
      # Add other env vars here if needed
```

**service_template требует:**
```env
BACKEND_IMAGE=ghcr.io/org/project-backend:latest
TG_BOT_IMAGE=ghcr.io/org/project-tg-bot:latest
POSTGRES_DB=project_name
POSTGRES_USER=postgres
POSTGRES_PASSWORD=...
TELEGRAM_BOT_TOKEN=...
```

---

## 3. Проблемы CI/CD в service_template

### 3.1 GitHub Actions секреты

**Файл:** `template/.github/workflows/main.yml.jinja:82-103`

CI workflow требует секреты для деплоя:
```yaml
deploy:
  if: success() && secrets.DEPLOY_HOST != ''
  steps:
    - uses: appleboy/ssh-action@v1.0.0
      with:
        host: ${{ secrets.DEPLOY_HOST }}
        username: ${{ secrets.DEPLOY_USER }}
        key: ${{ secrets.DEPLOY_SSH_KEY }}
        port: ${{ secrets.DEPLOY_PORT || '22' }}
```

**Требуемые секреты:**

| Секрет | Описание | Источник значения |
|--------|----------|-------------------|
| `DEPLOY_HOST` | IP сервера | `Server.public_ip` из allocated_resources |
| `DEPLOY_USER` | SSH пользователь | Всегда `root` |
| `DEPLOY_SSH_KEY` | Приватный SSH ключ | Из orchestrator или генерировать |
| `DEPLOY_PORT` | SSH порт | `22` (опционально) |
| `DEPLOY_PROJECT_PATH` | Путь на сервере | `/opt/services/{project_name}` |
| `DEPLOY_COMPOSE_FILES` | Compose файлы | `infra/compose.base.yml infra/compose.prod.yml` |

### 3.2 Нет механизма установки секретов

GitHub API требует шифрования секретов через libsodium. Текущий `GitHubAppClient` не умеет это делать.

**Нужно добавить метод:**
```python
async def set_repository_secret(
    self, owner: str, repo: str, name: str, value: str
) -> None:
    """Set an encrypted repository secret."""
    # 1. Получить public key репозитория
    # 2. Зашифровать value через libsodium
    # 3. PUT /repos/{owner}/{repo}/actions/secrets/{name}
```

### 3.3 Переменные окружения для образов

**Файл:** `template/infra/compose.prod.yml.jinja:7`

```yaml
image: ${BACKEND_IMAGE:?Set BACKEND_IMAGE to the published backend image}
```

Эти переменные нужно передавать при запуске docker compose на сервере. Варианты:

1. **Через .env файл** - прописать в `/opt/services/{project}/.env`
2. **Через environment в compose** - но это усложняет обновление
3. **Через GitHub secrets** - тогда CI должен передавать их при деплое

**Рекомендация:** Использовать convention over configuration:
```bash
# Имена образов по конвенции
BACKEND_IMAGE=ghcr.io/${GITHUB_REPOSITORY}-backend:latest
```

Это уже работает в CI (см. `main.yml.jinja:67`), нужно только прописать в .env.prod на сервере.

---

## 4. План доработок

### 4.1 Фаза 1: Минимальная публикация (MVP)

**Цель:** После engineering проект публикуется и доступен по IP:PORT

#### 4.1.1 Обновить Dockerfile langgraph

```dockerfile
# Добавить после COPY services/langgraph
COPY services/infrastructure ./services/infrastructure

# Установить Ansible
RUN pip install ansible
```

#### 4.1.2 Переписать deploy_project.yml

```yaml
---
- name: Deploy project via Docker Compose
  hosts: all
  gather_facts: no
  vars:
    project_dir: "/opt/services/{{ project_name }}"
    repo_url: "https://{{ github_token }}@github.com/{{ repo_full_name }}.git"

  tasks:
    - name: Ensure project directory exists
      file:
        path: "{{ project_dir }}"
        state: directory
        mode: '0755'

    - name: Clone or update repository
      git:
        repo: "{{ repo_url }}"
        dest: "{{ project_dir }}"
        version: main
        force: yes

    - name: Create .env file for production
      copy:
        dest: "{{ project_dir }}/.env"
        content: |
          ENVIRONMENT=production
          APP_ENV=production
          PORT={{ service_port }}
          BACKEND_IMAGE=ghcr.io/{{ repo_full_name }}-backend:latest
          TG_BOT_IMAGE=ghcr.io/{{ repo_full_name }}-tg-bot:latest
          FRONTEND_IMAGE=ghcr.io/{{ repo_full_name }}-frontend:latest
          NOTIFICATIONS_WORKER_IMAGE=ghcr.io/{{ repo_full_name }}-notifications-worker:latest
          POSTGRES_DB={{ project_name | replace('-', '_') }}
          POSTGRES_USER=postgres
          POSTGRES_PASSWORD={{ lookup('password', '/dev/null length=32 chars=ascii_letters,digits') }}
          {% if telegram_token is defined %}
          TELEGRAM_BOT_TOKEN={{ telegram_token }}
          {% endif %}
        mode: '0600'

    - name: Create .env.prod for infra
      copy:
        dest: "{{ project_dir }}/infra/.env.prod"
        content: |
          ENVIRONMENT=production
        mode: '0600'

    - name: Login to GitHub Container Registry
      shell: echo "{{ github_token }}" | docker login ghcr.io -u {{ repo_full_name.split('/')[0] }} --password-stdin
      register: login_result
      changed_when: login_result.rc == 0

    - name: Pull and start services
      shell: |
        cd {{ project_dir }}
        docker compose -f infra/compose.base.yml -f infra/compose.prod.yml pull
        docker compose -f infra/compose.base.yml -f infra/compose.prod.yml up -d --remove-orphans
      register: compose_result

    - name: Prune unused images
      command: docker image prune -f
      failed_when: false
```

#### 4.1.3 Добавить SSH ключ в контейнер

**Вариант A: Volume mount (рекомендуется)**

```yaml
# docker-compose.yml
services:
  langgraph:
    volumes:
      - ${SSH_KEYS_PATH:-/opt/secrets/ssh}:/root/.ssh:ro
```

**Вариант B: Из базы данных**

Добавить метод в `devops.py`:
```python
async def get_server_ssh_key(server_handle: str) -> str:
    """Get and decrypt SSH key for server."""
    server = await api_client.get_server(server_handle)
    encrypted_key = server.get("ssh_key_enc")
    if not encrypted_key:
        raise ValueError(f"No SSH key for server {server_handle}")
    return decrypt_ssh_key(encrypted_key)  # SOPS/AGE
```

#### 4.1.4 Добавить открытие порта на firewall

В `deploy_project.yml`:
```yaml
- name: Open service port in UFW
  ufw:
    rule: allow
    port: "{{ service_port }}"
    proto: tcp
```

### 4.2 Фаза 2: CI/CD интеграция

**Цель:** При пуше в main образ автоматически обновляется на сервере

#### 4.2.1 Добавить метод set_repository_secret

**Файл:** `shared/clients/github.py`

```python
from nacl import encoding, public

async def set_repository_secret(
    self, owner: str, repo: str, name: str, value: str
) -> None:
    """Set an encrypted repository secret using GitHub API."""
    token = await self.get_token(owner, repo)
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github+json",
    }

    async with httpx.AsyncClient() as client:
        # 1. Get repository public key
        resp = await client.get(
            f"https://api.github.com/repos/{owner}/{repo}/actions/secrets/public-key",
            headers=headers,
        )
        resp.raise_for_status()
        key_data = resp.json()
        public_key = key_data["key"]
        key_id = key_data["key_id"]

        # 2. Encrypt the secret using libsodium
        public_key_bytes = public.PublicKey(
            public_key.encode("utf-8"),
            encoding.Base64Encoder()
        )
        sealed_box = public.SealedBox(public_key_bytes)
        encrypted = sealed_box.encrypt(value.encode("utf-8"))
        encrypted_value = encoding.Base64Encoder().encode(encrypted).decode("utf-8")

        # 3. Create or update the secret
        resp = await client.put(
            f"https://api.github.com/repos/{owner}/{repo}/actions/secrets/{name}",
            headers=headers,
            json={
                "encrypted_value": encrypted_value,
                "key_id": key_id,
            },
        )
        resp.raise_for_status()
```

**Зависимость:** `pip install pynacl`

#### 4.2.2 Создать инструмент настройки CI секретов

**Файл:** `services/langgraph/src/tools/github_secrets.py`

```python
@tool
async def setup_deployment_secrets(
    repo_full_name: Annotated[str, "Full repository name (org/repo)"],
    server_ip: Annotated[str, "Server IP address"],
    service_port: Annotated[int, "Service port"],
    project_name: Annotated[str, "Project name (kebab-case)"],
    ssh_private_key: Annotated[str, "SSH private key for deployment"],
) -> str:
    """Configure GitHub Actions secrets for automated deployment.

    Sets up the following secrets:
    - DEPLOY_HOST: Server IP
    - DEPLOY_USER: SSH user (root)
    - DEPLOY_SSH_KEY: SSH private key
    - DEPLOY_PROJECT_PATH: /opt/services/{project_name}
    - DEPLOY_COMPOSE_FILES: infra/compose.base.yml infra/compose.prod.yml
    """
    owner, repo = repo_full_name.split("/")
    client = GitHubAppClient()

    secrets = {
        "DEPLOY_HOST": server_ip,
        "DEPLOY_USER": "root",
        "DEPLOY_SSH_KEY": ssh_private_key,
        "DEPLOY_PROJECT_PATH": f"/opt/services/{project_name}",
        "DEPLOY_COMPOSE_FILES": "infra/compose.base.yml infra/compose.prod.yml",
    }

    for name, value in secrets.items():
        await client.set_repository_secret(owner, repo, name, value)

    return f"Configured {len(secrets)} deployment secrets for {repo_full_name}"
```

#### 4.2.3 Вызывать setup_deployment_secrets после первого деплоя

В `devops.py` после успешного Ansible:

```python
# После create_service_deployment_record
await setup_ci_secrets(
    repo_full_name=repo_full_name,
    server_ip=target_server_ip,
    port=target_port,
    project_name=project_name,
    ssh_key=ssh_key,  # Нужно получить откуда-то
)
```

### 4.3 Фаза 3: Resource-aware selection (опционально для MVP)

**Цель:** Учитывать нагрузку при выборе сервера

#### 4.3.1 Текущая реализация (достаточна для MVP)

```python
# servers.py:find_suitable_server
# Уже фильтрует по available_ram и available_disk
# Выбирает сервер с максимальным available_ram
```

#### 4.3.2 Улучшение (post-MVP)

Добавить учёт количества сервисов:
```python
# Prefer servers with fewer services
weight = available_ram * 0.7 + (MAX_SERVICES - current_services) * 0.3
```

---

## 5. Схема координации между проектами

### 5.1 service_template

**Изменения не требуются для MVP.** CI workflow уже генерирует правильные образы и деплоит их, если секреты настроены.

**Опционально (улучшение UX):**
- Добавить health check endpoint в `main.yml.jinja` после деплоя
- Добавить Slack/Telegram notification при успешном деплое

### 5.2 codegen_orchestrator

| Файл | Изменение |
|------|-----------|
| `services/langgraph/Dockerfile` | Добавить COPY infrastructure, установить ansible |
| `services/infrastructure/ansible/playbooks/deploy_project.yml` | Переписать под новый формат compose |
| `docker-compose.yml` | Добавить volume для SSH ключей |
| `shared/clients/github.py` | Добавить `set_repository_secret` |
| `services/langgraph/src/tools/github_secrets.py` | Новый файл - инструмент настройки секретов |
| `services/langgraph/src/nodes/devops.py` | Вызывать setup_ci_secrets после деплоя |

### 5.3 Последовательность выполнения

```
1. [orchestrator] Zavhoz allocates server:port
2. [orchestrator] Engineering completes (repo has code)
3. [orchestrator] DevOps runs deploy_project.yml
   - Клонирует репо на сервер
   - Создаёт .env с образами и секретами
   - Запускает docker compose
4. [orchestrator] DevOps настраивает GitHub secrets
   - DEPLOY_HOST, DEPLOY_USER, DEPLOY_SSH_KEY, etc.
5. [github actions] При следующем пуше в main
   - CI билдит образы
   - Пушит в ghcr.io
   - Деплоит на сервер (docker compose pull && up)
```

---

## 6. Риски и митигация

| Риск | Вероятность | Влияние | Митигация |
|------|-------------|---------|-----------|
| SSH ключ не работает | Средняя | Высокое | Добавить проверку SSH до деплоя |
| ghcr.io недоступен | Низкая | Высокое | Retry с exponential backoff |
| Порт занят на сервере | Средняя | Среднее | Проверять порт перед allocation |
| compose.prod.yml не совместим | Низкая | Высокое | E2E тесты перед релизом |
| Secrets API rate limit | Низкая | Низкое | Batch установку секретов |

---

## 7. Зависимости

### 7.1 Python packages

```
# requirements.txt additions
ansible>=8.0.0
pynacl>=1.5.0  # Для шифрования GitHub secrets
```

### 7.2 Системные

- SSH ключ для доступа к серверам (должен быть в volume или базе)
- GitHub App с правами `secrets: write` для репозиториев

---

## 8. Оценка трудозатрат

| Задача | Сложность | Приоритет |
|--------|-----------|-----------|
| Обновить Dockerfile | Низкая | P0 |
| Переписать deploy_project.yml | Средняя | P0 |
| Настроить SSH в контейнере | Низкая | P0 |
| Добавить set_repository_secret | Средняя | P1 |
| Создать setup_deployment_secrets tool | Низкая | P1 |
| Интегрировать в devops.py | Низкая | P1 |
| E2E тестирование | Высокая | P0 |

**P0 (MVP):** Минимально для работающего первого деплоя
**P1 (CI):** Автоматическое обновление при пуше

---

## 9. Проверочный чеклист

### 9.1 MVP Ready

- [ ] DevOps нода успешно деплоит проект
- [ ] Проект доступен по http://IP:PORT
- [ ] Docker compose запускается без ошибок
- [ ] .env содержит все необходимые переменные
- [ ] Порт открыт в UFW

### 9.2 CI Ready

- [ ] GitHub secrets установлены
- [ ] При пуше в main билдятся образы
- [ ] Образы пушатся в ghcr.io
- [ ] Сервер подтягивает новые образы
- [ ] Сервисы перезапускаются без downtime

---

## 10. Приложение: Примеры конфигураций

### 10.1 Пример .env для production

```env
# Application
ENVIRONMENT=production
APP_ENV=production
APP_SECRET_KEY=generated-32-char-secret

# Database
POSTGRES_HOST=db
POSTGRES_PORT=5432
POSTGRES_DB=my_project
POSTGRES_USER=postgres
POSTGRES_PASSWORD=generated-32-char-password
DATABASE_URL=postgresql+psycopg://postgres:password@db:5432/my_project

# Redis
REDIS_URL=redis://redis:6379

# Images (for compose.prod.yml)
BACKEND_IMAGE=ghcr.io/org/my-project-backend:latest
TG_BOT_IMAGE=ghcr.io/org/my-project-tg-bot:latest

# Telegram (if applicable)
TELEGRAM_BOT_TOKEN=123456:ABC...
```

### 10.2 Структура на сервере после деплоя

```
/opt/services/my-project/
├── .env                    # Production secrets
├── .git/                   # Cloned repo
├── infra/
│   ├── compose.base.yml
│   ├── compose.prod.yml
│   └── .env.prod
├── services/
│   ├── backend/
│   └── tg_bot/
└── shared/
```

---

*Документ создан: 2024-12-29*
*Автор: Claude Code Analysis*
