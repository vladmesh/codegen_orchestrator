# Repo Provisioning Fix

> **Проблема:** После удаления модуля `preparer`, GitHub репозиторий не создаётся при создании нового проекта. `DeveloperNode` падает с 404 при попытке получить токен для несуществующего репо.

## Контекст

### Текущий флоу (сломан)
1. `orchestrator project create` → создаёт запись в PostgreSQL
2. `engineering trigger` → запускает Engineering Subgraph
3. `DeveloperNode` → пытается `get_token(owner, repo)` → **💥 404 Not Found**

### Причины проблемы
1. **Репо не создаётся** — после удаления `preparer` никто не создаёт GitHub репозиторий
2. **Owner захардкожен** — в `developer.py` строка 73: `owner = "vladmesh"`
3. **Токен запрашивается для несуществующего репо** — `get_installation_id()` требует существующий репо

---

## Решение

### Принятые решения
1. ✅ Создание репо в API при `POST /api/projects/` — централизовано, любой клиент получит репо автоматически
2. ✅ Использовать `get_first_org_installation()` для определения org — уже есть в `GitHubAppClient`
3. ✅ Сразу прокидывать секреты из `project.config.secrets` в GitHub Actions
4. ✅ Добавить `.project.yaml` с описанием проекта для восстановления/отладки

---

## План реализации

### Фаза 1: Расширение GitHubAppClient

**Файл:** `shared/clients/github.py`

#### 1.1 Добавить метод `provision_project_repo()`

```python
async def provision_project_repo(
    self,
    name: str,
    description: str = "",
    project_spec: dict | None = None,
    secrets: dict[str, str] | None = None,
) -> GitHubRepository:
    """Create repo with initial config and secrets.
    
    Org is auto-detected from GitHub App installation.
    
    Args:
        name: Repository name (will be sanitized to kebab-case)
        description: Repository description
        project_spec: Project specification to save as .project.yaml
        secrets: Secrets to set in GitHub Actions (e.g., TELEGRAM_TOKEN)
    
    Returns:
        Created repository info
    """
    # 1. Auto-detect org from GitHub App installation
    installation = await self.get_first_org_installation()
    org = installation["org"]
    
    # 2. Sanitize repo name
    repo_name = name.lower().replace(" ", "-").replace("_", "-")
    
    # 3. Create repository
    repo = await self.create_repo(org, repo_name, description, private=True)
    
    # 4. Add .project.yaml if spec provided
    if project_spec:
        import yaml
        config_content = yaml.dump(project_spec, default_flow_style=False, allow_unicode=True)
        await self.create_or_update_file(
            owner=org,
            repo=repo_name,
            path=".project.yaml",
            content=config_content,
            message="chore: add project configuration",
        )
    
    # 5. Set secrets if provided
    if secrets:
        await self.set_repository_secrets(org, repo_name, secrets)
    
    logger.info(
        "project_repo_provisioned",
        org=org,
        repo=repo_name,
        secrets_count=len(secrets) if secrets else 0,
    )
    
    return repo
```

#### 1.2 Добавить зависимость `pyyaml`
- Проверить, есть ли уже в `shared/pyproject.toml`
- Если нет — добавить

---

### Фаза 2: Интеграция в API

**Файл:** `services/api/src/routes/projects.py`

#### 2.1 Модифицировать `POST /api/projects/`

```python
@router.post("/", response_model=ProjectResponse)
async def create_project(
    project: ProjectCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Create a new project with GitHub repository."""
    
    # 1. Create DB record
    db_project = Project(
        id=project.id or str(uuid.uuid4()),
        name=project.name,
        owner_id=current_user.id,
        status="created",
        config=project.config or {},
    )
    db.add(db_project)
    await db.flush()  # Get ID before GitHub call
    
    # 2. Provision GitHub repo
    try:
        github_client = GitHubAppClient()
        repo = await github_client.provision_project_repo(
            name=project.name,
            description=project.description or f"Project: {project.name}",
            project_spec={
                "id": str(db_project.id),
                "name": project.name,
                "description": project.description,
                "created_at": datetime.now(UTC).isoformat(),
                "owner": current_user.telegram_id,
            },
            secrets=project.config.get("secrets") if project.config else None,
        )
        
        # 3. Update project with repo URL
        db_project.repository_url = repo.html_url
        db_project.config["github_repo_id"] = repo.id
        
    except Exception as e:
        logger.error("github_repo_creation_failed", error=str(e), project_name=project.name)
        # Decide: fail the request or continue without repo?
        # For now, continue but mark status
        db_project.status = "repo_failed"
        db_project.config["repo_error"] = str(e)
    
    await db.commit()
    await db.refresh(db_project)
    
    return db_project
```

#### 2.2 Обработка ошибок
- Если GitHub API недоступен — создать проект без репо, пометить статус
- Добавить retry механизм или background task для повторной попытки

---

### Фаза 3: Исправление DeveloperNode

**Файл:** `services/langgraph/src/nodes/developer.py`

#### 3.1 Убрать хардкод `vladmesh`

```python
# Было:
owner = "vladmesh"  # TODO: get from settings or project config

# Стало:
installation = await github_client.get_first_org_installation()
owner = installation["org"]
```

#### 3.2 Использовать `get_org_token()` вместо `get_token()`

```python
# Было:
access_token = await github_client.get_token(owner, repo_name)

# Стало (если репо может не существовать):
access_token = await github_client.get_org_token(owner)

# Или (если репо гарантированно существует после API):
access_token = await github_client.get_token(owner, repo_name)
```

#### 3.3 Получать repo info из project_spec

```python
# Репо уже должен быть создан через API
repo_info = state.get("repo_info", {})
repo_full_name = repo_info.get("full_name")

if not repo_full_name:
    # Fallback: построить из org + project name
    installation = await github_client.get_first_org_installation()
    owner = installation["org"]
    repo_name = project_name.lower().replace(" ", "-")
    repo_full_name = f"{owner}/{repo_name}"
```

#### 3.4 Обновить `_build_task_message()`

Убрать инструкцию "Create GitHub repository if it doesn't exist" — репо уже создан.

---

### Фаза 4: Убрать остатки preparer

#### 4.1 Удалить deprecated поля из state
- `preparer_commit_sha` → переименовать в `commit_sha` или убрать
- `repo_prepared` → убрать, репо всегда готов после API

#### 4.2 Обновить тесты
- `test_architect_routing.py` — использует `route_after_preparer`
- Переименовать/обновить тесты под новую логику

---

### Фаза 5: Тестирование

#### 5.1 Unit тесты
- [ ] `test_provision_project_repo()` — мок GitHub API
- [ ] `test_create_project_with_repo()` — мок GitHubAppClient

#### 5.2 Integration тесты
- [ ] Создать проект через API → проверить репо на GitHub
- [ ] Запустить engineering flow → проверить что DeveloperNode работает

#### 5.3 E2E тест
- [ ] Telegram: "создай проект test-bot" → проверить полный флоу

---

## Чеклист

### Фаза 1: GitHubAppClient
- [ ] Добавить `provision_project_repo()` в `shared/clients/github.py`
- [ ] Проверить/добавить `pyyaml` dependency
- [ ] Написать unit тест

### Фаза 2: API
- [ ] Модифицировать `POST /api/projects/`
- [ ] Добавить error handling для GitHub failures
- [ ] Обновить OpenAPI schema

### Фаза 3: DeveloperNode
- [ ] Убрать хардкод `vladmesh`
- [ ] Использовать `get_first_org_installation()`
- [ ] Обновить `_build_task_message()` — убрать создание репо
- [ ] Использовать `repo_info` из state

### Фаза 4: Cleanup
- [ ] Убрать/переименовать `preparer_commit_sha`
- [ ] Обновить тесты

### Фаза 5: Verification
- [ ] Unit тесты проходят
- [ ] Integration тест: API → GitHub
- [ ] E2E: Telegram → полный флоу

---

## Риски и mitigation

| Риск | Mitigation |
|------|------------|
| GitHub API rate limits | Использовать Installation token (5000 req/hr) |
| GitHub API недоступен | Создать проект с `status=repo_failed`, retry позже |
| Дублирование репо | Проверять существование перед созданием |
| Секреты не установились | Логировать, но не блокировать создание |

---

## Связанные файлы

- `shared/clients/github.py` — GitHubAppClient
- `services/api/src/routes/projects.py` — API endpoint
- `services/langgraph/src/nodes/developer.py` — DeveloperNode
- `services/langgraph/src/workers/engineering_worker.py` — Engineering worker
- `shared/cli/src/orchestrator/commands/project.py` — CLI (без изменений)
