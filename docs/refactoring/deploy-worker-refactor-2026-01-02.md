# Deploy Worker Refactoring — 2026-01-02

## Проблема

`deploy_worker` выполнял `ansible-playbook` локально через `subprocess`, нарушая микросервисную архитектуру. LangGraph контейнер содержал установку Ansible, которая должна была находиться в infrastructure-worker.

## Решение

Делегирование выполнения Ansible в infrastructure-worker через Redis очередь.

## Изменения

### 1. Shared Schemas (Новые файлы)

**`shared/schemas/deployment_jobs.py`**
- `DeploymentJobRequest` — схема запроса на deployment
- `DeploymentJobResult` — схема результата deployment

**`shared/queues.py`**
- Добавлена очередь `ANSIBLE_DEPLOY_QUEUE = "ansible:deploy:queue"`

### 2. Infrastructure Worker (Расширение)

**`services/infrastructure-worker/src/provisioner/deployment_executor.py`** (новый файл)
- `run_deployment_playbook()` — выполнение Ansible playbook для deployment
- Аналогично `ansible_runner.py`, но для deployment задач

**`services/infrastructure-worker/src/main.py`** (обновлен)
- Обработка двух типов jobs: `provision` и `deploy`
- Чтение из двух очередей: `PROVISIONER_QUEUE` и `ANSIBLE_DEPLOY_QUEUE`
- Маршрутизация к соответствующим обработчикам
- Публикация результатов в Redis: `deploy:result:{request_id}`

### 3. LangGraph (Рефакторинг)

**`services/langgraph/src/tools/devops_delegation.py`** (новый файл)
- `delegate_ansible_deploy()` — tool для делегирования deployment
- Подготовка deployment job
- Отправка в `ANSIBLE_DEPLOY_QUEUE`
- Polling результата из Redis с timeout

**`services/langgraph/src/subgraphs/devops/nodes.py`** (обновлен)
- `DeployerNode.run()` — использует `delegate_ansible_deploy`
- Перенесены helper функции:
  - `_create_service_deployment_record()` — создание записи в БД
  - `_setup_ci_secrets()` — настройка GitHub Actions secrets
- Post-deployment операции выполняются после получения результата от infrastructure-worker

**`services/langgraph/src/tools/__init__.py`** (обновлен)
- Импорт изменен: `run_ansible_deploy` → `delegate_ansible_deploy`

**`services/langgraph/tests/unit/test_devops_ci_secrets.py`** (обновлен)
- Импорт обновлен: `src.tools.devops_tools` → `src.subgraphs.devops.nodes`
- Все patch пути обновлены

### 4. Cleanup (Удалено)

❌ **`services/langgraph/src/tools/devops_tools.py`** — удален полностью
❌ **`services/langgraph/Dockerfile`** — удалена строка `RUN ansible-galaxy collection install community.general`
❌ **`docker-compose.yml`** — удален volume mount `./services/infrastructure-worker/ansible:/app/services/infrastructure/ansible:delegated`
❌ **`services/langgraph/src/config/constants.py`** — удалены `Paths.ANSIBLE_PLAYBOOKS` и `Paths.playbook()`

## Архитектура после рефакторинга

```
┌─────────────────────────────────────────────────────────────┐
│ DeployerNode (langgraph)                                    │
│                                                              │
│  1. delegate_ansible_deploy tool                            │
│     ├─ Prepare deployment job                               │
│     ├─ Get GitHub token                                     │
│     ├─ Send to Redis: ansible:deploy:queue                  │
│     └─ Poll result from Redis: deploy:result:{request_id}   │
│                                                              │
│  2. Post-deployment operations (if success)                 │
│     ├─ _create_service_deployment_record()  → API           │
│     ├─ _setup_ci_secrets()                  → GitHub        │
│     └─ Update project status                → API           │
└─────────────────────────────────────────────────────────────┘
                                │
                                │ Redis Queue
                                ▼
┌─────────────────────────────────────────────────────────────┐
│ Infrastructure Worker                                        │
│                                                              │
│  1. Consume from ansible:deploy:queue                        │
│  2. process_deployment_job()                                 │
│     └─ run_deployment_playbook()                            │
│        ├─ Build ansible-playbook command                    │
│        ├─ Execute subprocess                                │
│        └─ Return result                                     │
│  3. Publish result to deploy:result:{request_id}             │
└─────────────────────────────────────────────────────────────┘
```

## Разделение ответственности

| Компонент | Ответственность |
|-----------|----------------|
| **DeployerNode** | Orchestration: подготовка данных, post-deployment операции (DB records, GitHub secrets) |
| **infrastructure-worker** | Execution: запуск Ansible playbook, управление процессом |
| **Redis** | Communication: очередь jobs, хранение результатов (TTL 1 час) |

## Преимущества

1. ✅ **Архитектурная чистота**: LangGraph не содержит Ansible
2. ✅ **Меньший образ**: langgraph Dockerfile без ansible-galaxy install
3. ✅ **Переиспользование**: infrastructure-worker обрабатывает provisioning И deployment
4. ✅ **Изоляция ошибок**: сбой Ansible не роняет langgraph worker
5. ✅ **Retry capabilities**: можно переотправить job без перезапуска всего subgraph
6. ✅ **Масштабируемость**: можно запустить несколько infrastructure-worker для параллельных deployments

## Testing

- ✅ Lint: `make lint` — All checks passed
- ✅ Unit tests: `make test-langgraph-unit` — 85 passed
- ✅ Coverage: 39% (без изменений)

## Migration Notes

При обновлении production:
1. Пересобрать все образы: `make build`
2. infrastructure-worker автоматически начнет слушать обе очереди
3. Старые deployment jobs (если есть в DEPLOY_QUEUE) продолжат работать через deploy_worker
4. Новые deployments через DevOps subgraph будут использовать новый паттерн

## Follow-up Tasks

1. Опционально: мигрировать `deploy_worker.py` на использование delegation (сейчас он все еще запускает DevOps subgraph напрямую)
2. Опционально: добавить metrics для deployment job processing time
3. Опционально: реализовать callback stream для real-time progress updates (сейчас только polling)

## Related Issues

- Resolves backlog item: "Refactor Deploy Worker (Architectural Debt)"
- Unblocks: cleaner Docker builds, smaller image sizes
- Enables: future separation of infrastructure-worker into dedicated deployment service if needed
