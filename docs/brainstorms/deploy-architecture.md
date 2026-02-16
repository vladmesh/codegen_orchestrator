# Brainstorm: Архитектура деплоя — проблемы и решения

> **Дата**: 2026-02-15
> **Контекст**: E2E тест полного цикла (PO → Engineering → Deploy) выявил, что задеплоенный бот `reverse-bot` не работал, несмотря на зелёные CI-джобы в GitHub. Расследование вскрыло системные проблемы в деплой-инфраструктуре.

---

## Обнаруженные проблемы

### 1. Ansible использует dev-compose вместо prod

**Файл**: `services/infra-service/ansible/playbooks/deploy_project.yml:119`

```yaml
cmd: "{{ compose_cmd_dev }} up -d --build"
```

`compose_cmd_dev` = `docker compose -f compose.base.yml -f compose.dev.yml --build`

**Что происходит**:
- Ansible пытается собрать Docker-образы из исходников прямо на VPS (`--build`)
- Вместо `compose.prod.yml` (pull готовых образов из GHCR) используется `compose.dev.yml`
- Сборка `tg_bot` на сервере падает (`exit_code=2`) из-за нехватки ресурсов / отсутствия build context
- Контейнеры не стартуют, бот мёртв

**Факт**: ручной запуск с `compose.prod.yml` без `--build` поднял всё за секунды — образы уже были в GHCR.

### 2. Двойной деплой

После CI оркестратор запускает **два** деплой-процесса:

| # | Путь | Триггер | Когда |
|---|------|---------|-------|
| 1 | **Ansible** (DevOps subgraph → infra-service) | engineering_worker auto-triggers после CI | Всегда (если `skip_deploy=False`) |
| 2 | **GitHub Actions** (`main.yml` deploy job) | `workflow_dispatch` с `deploy_host` | Если CI secrets настроены |

Текущая ситуация: второй путь не срабатывает на `push` events (только на `workflow_dispatch`), но `_setup_ci_secrets()` записывает `DEPLOY_HOST` в GitHub — создавая потенциал для конфликта при ручном запуске.

### 3. Несовпадение путей

| Источник | Путь на сервере |
|----------|----------------|
| Ansible playbook (`deploy_dir`) | `/opt/apps/{{ project_name }}` |
| `_setup_ci_secrets()` (`DEPLOY_PROJECT_PATH`) | `/opt/services/{{ project_name }}` |
| `deployment_info` в БД | `/opt/services/reverse-bot` |

GitHub Actions deploy (если запустится) сделает `cd /opt/services/reverse-bot` — директории не существует. Ansible деплоит в `/opt/apps/`.

### 4. CI secrets неполные для GitHub Actions deploy

`_setup_ci_secrets()` записывает 5 секретов:
```
DEPLOY_HOST, DEPLOY_USER, DEPLOY_SSH_KEY, DEPLOY_PROJECT_PATH, DEPLOY_COMPOSE_FILES
```

`main.yml` ожидает ещё: `APP_SECRET_KEY`, `POSTGRES_PASSWORD`, `TELEGRAM_BOT_TOKEN`, `REDIS_URL` и т.д. Они пустые → `.env` на сервере будет без критических значений → бот стартует но не работает.

### 5. Health check маскирует ошибки

```yaml
# deploy_project.yml:174-181
- name: Check if service responds
  uri:
    url: "http://localhost:{{ service_port }}/health"
    status_code: [200, 404]  # 404 = "ок"?!
  ignore_errors: yes          # даже если упал — не ошибка
```

Ansible **всегда** возвращает success. Оркестратор записывает `status: active` даже если бот мёртв.

### 6. Секреты нельзя полностью предсказать

Шаблон генерирует известные секреты (`POSTGRES_PASSWORD`, `TELEGRAM_BOT_TOKEN`). Но developer agent может добавить новые (`OPENAI_API_KEY`, `SENTRY_DSN`, `WEBHOOK_SECRET`). Кто классифицирует — infra / user / computed — и откуда брать значение?

Текущий `env_analyzer.py` решает это через LLM при полном DevOps-прогоне. Но для `action=feature` (доработка) полный DevOps-прогон — overkill.

---

## Анализ двух деплой-путей

### Путь A: Ansible (текущий основной)

```
Engineering Worker → DEPLOY_QUEUE → Deploy Worker → DevOps Subgraph:
  ResourceAllocator → EnvAnalyzer → SecretResolver → ReadinessCheck → DeployerNode
    → wait for CI (main.yml build) → delegate_ansible_deploy → infra-service
    → Ansible: clone repo, write .env, docker compose up
```

**Плюсы**: полный контроль, может резолвить секреты, создавать DNS, firewall
**Минусы**: overkill для feature updates, баги с compose.dev/prod, медленный

### Путь B: GitHub Actions (`main.yml`)

```
Push to main → GitHub Actions:
  build-and-push: build Docker images → GHCR
  deploy: SSH to server → generate .env → docker compose pull && up -d
```

**Плюсы**: стандартный CI/CD, видно в GitHub UI, не требует оркестратора
**Минусы**: секреты должны быть в GitHub (не все известны), `.env` генерится из шаблона (хрупко)

---

## Варианты решения

### Вариант 1: Разделить config и deploy

**Принцип**: `.env` на сервере — source of truth, управляется только оркестратором. CI/CD только тянет образы и перезапускает.

**`action=create` (первый деплой)**:
```
DevOps subgraph (полный):
  1. Allocate server
  2. Resolve secrets (env_analyzer + SecretResolver)
  3. SSH: write .env to server
  4. SSH: docker compose pull && up -d
  5. Setup CI secrets (только DEPLOY_HOST/USER/KEY/PATH — без сервисных секретов)
```

**`action=feature` (доработка)**:
```
Engineering Worker → Developer push → CI green →
  1. Diff .env.example (old vs new commit) → новые переменные?
     - Нет → skip, деплой через main.yml (pull + restart, .env не трогаем)
     - Да → callback в PO: "Нужен OPENAI_API_KEY" → user provides → orchestrator SSH writes .env → deploy
  2. main.yml deploy: cd project && docker compose pull && up -d
     (НЕ генерирует .env — он уже на сервере)
```

**`main.yml` упрощается до**:
```yaml
deploy:
  steps:
    - name: Deploy
      uses: appleboy/ssh-action@v1
      with:
        script: |
          cd "${{ secrets.DEPLOY_PROJECT_PATH }}"
          docker compose -f infra/compose.base.yml -f infra/compose.prod.yml pull
          docker compose -f infra/compose.base.yml -f infra/compose.prod.yml up -d --remove-orphans
```

**Плюсы**:
- Чистое разделение: config (orchestrator) vs deploy (CI/CD)
- Новые секреты обнаруживаются через diff, а не через гадание
- `main.yml` тупой и надёжный — не может сломать `.env`
- Ansible нужен только для провижинга сервера (Docker, firewall, users)

**Минусы**:
- Нужен механизм diff `.env.example` в engineering worker
- `.env` на сервере можно случайно сломать при SSH-доступе

### Вариант 2: Всё через GitHub Actions

**Принцип**: все секреты в GitHub Secrets. `main.yml` генерит `.env` и деплоит.

**`action=create`**:
```
DevOps subgraph → resolve ALL secrets → write ALL to GitHub Secrets →
  trigger workflow_dispatch(main.yml) → build + deploy
```

**`action=feature`**:
```
Engineering Worker → skip_deploy=True → main.yml auto-deploy on push
```

**Обнаружение новых секретов**: env_analyzer при каждом деплое читает `.env.example` из repo, классифицирует, резолвит.

**Плюсы**: один путь деплоя, всё в GitHub UI
**Минусы**:
- Нужно знать ВСЕ секреты заранее и класть в GitHub
- `main.yml` шаблон хрупкий — Jinja рендерит его при scaffold, потом не меняется
- Если developer добавил переменную — она не появится в `main.yml` автоматически
- Каждый push в main = полный деплой (может быть нежелательно)

### Вариант 3: Всё через Ansible (оркестратор)

**Принцип**: GitHub Actions только билдит образы. Деплой всегда через оркестратор.

**`action=create`**: полный DevOps subgraph (как сейчас, но с исправлениями)
**`action=feature`**: облегчённый деплой — SSH: `pull + restart` (без полного Ansible playbook)

**Плюсы**: полный контроль, secrets resolution в одном месте
**Минусы**: engineering worker должен тригерить деплой даже для мелких изменений, зависимость от оркестратора для каждого деплоя

---

## Рекомендация: Вариант 1

Разделение config и deploy — самый чистый подход:

1. **Оркестратор владеет `.env`** на сервере (SSH write)
2. **CI/CD владеет деплоем** (pull + restart)
3. **Новые секреты** обнаруживаются через diff `.env.example`, а не через анализ всего кода
4. **Ansible** остаётся только для первоначального провижинга серверов

### Что нужно сделать

**Немедленные фиксы (баги)**:
- [ ] `deploy_project.yml`: `compose.dev.yml` → `compose.prod.yml`, убрать `--build`, добавить `pull`
- [ ] `deploy_project.yml`: убрать `ignore_errors: yes` с health check
- [ ] `_setup_ci_secrets()`: `/opt/services/` → `/opt/apps/` (или наоборот, но одинаково)
- [ ] `engineering_worker.py`: `skip_deploy=True` для `action=feature` (пока не реализован CI deploy)

**Архитектурные изменения**:
- [ ] Упростить `main.yml` deploy job: только `pull + up -d`, без генерации `.env`
- [ ] Добавить `main.yml` deploy trigger на `push` (сейчас только `workflow_dispatch`)
- [ ] Engineering worker: diff `.env.example` для обнаружения новых секретов
- [ ] PO tool: `update_project_env` — обновить `.env` на сервере через SSH
- [ ] DevOps subgraph: отдельный lightweight path для feature deploy (без полного прогона)

---

## Текущее состояние после расследования

- `reverse-bot` вручную поднят на `176.223.131.124` (`/opt/apps/reverse-bot`)
- Использована правильная команда: `compose.prod.yml` + `--profile tg` без `--build`
- Все 4 контейнера работают (backend, tg_bot, db, redis)
- Проект в БД оркестратора в статусе `error` (нужно обновить на `active`)
