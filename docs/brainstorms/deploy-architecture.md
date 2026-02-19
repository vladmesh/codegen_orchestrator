# Brainstorm: Архитектура деплоя

> **Начато**: 2026-02-15
> **Обновлено**: 2026-02-16
> **Статус**: Реализовано (iterations 1-9). См. [план](../plans/deploy-architecture.md) и [статус](../STATUS.md).
> **Контекст**: E2E тест (PO → Engineering → Deploy) выявил что `reverse-bot` не работал несмотря на зелёные CI-джобы. Расследование вскрыло системные проблемы.

---

## Обнаруженные проблемы

### Деплой
1. **Ansible использует `compose.dev.yml` + `--build`** вместо `compose.prod.yml` + `pull` — билд падает на VPS, контейнеры не стартуют
2. **Два конкурирующих деплой-пути** — Ansible (через оркестратор) и GitHub Actions (`main.yml`) — потенциальный конфликт
3. **Несовпадение путей**: Ansible → `/opt/apps/`, CI secrets → `/opt/services/`, БД → `/opt/services/`
4. **Health check маскирует ошибки**: `status_code: [200, 404]` + `ignore_errors: yes` → оркестратор пишет `active` для мёртвого бота

### Секреты
5. **CI secrets неполные** — `_setup_ci_secrets()` записывает только deploy-инфру (HOST/USER/KEY), application secrets (DATABASE_URL, TELEGRAM_BOT_TOKEN) не попадают в GitHub
6. **`DATABASE_URL` и `POSTGRES_PASSWORD` рассогласованы** — `_generate_infra_secret()` генерирует каждый независимо с разными паролями → Postgres не подключается
7. **Нет `.env.example` → тихий пропуск** — `env_analyzer` возвращает пустой `env_analysis` без ошибки, деплой идёт без `.env`
8. ~~**Секреты в БД без шифрования**~~ — **Решено**: Fernet encryption at rest (`shared/crypto.py`)
9. **Нет single source of truth** — часть секретов в БД, часть только на сервере, часть в GitHub Secrets

---

## Принятые решения

### Source of truth для секретов — БД оркестратора

- Все секреты хранятся в `project.config.secrets` (PostgreSQL)
- **Fernet encryption** at rest — шифруем перед записью, дешифруем при чтении
- Ключ шифрования: env var `SECRETS_ENCRYPTION_KEY` на langgraph сервисе
- Бэкап ключа: в `prod_infra` Ansible vault (зашифрован паролем из головы)
- Восстановление: `ansible-vault decrypt` → скопировал ключ → поднял оркестратор

### Деплой только через GitHub Actions (Ansible убираем)

**Ключевая идея**: на сервере не нужен исходный код — только compose файлы + `.env` + docker images из registry.

**Трюк с `DOTENV`**: оркестратор собирает весь `.env` в одну строку → base64 → один GitHub Secret. Workflow декодирует и пишет файл. Не нужно перечислять переменные поимённо, шаблон никогда не устаревает.

**CI workflow (идемпотентный, одинаковый для первого и последующих деплоев)**:
```yaml
deploy:
  steps:
    - uses: actions/checkout@v4
    - name: Copy compose files
      uses: appleboy/scp-action@v1
      with:
        source: "infra/compose.base.yml,infra/compose.prod.yml"
        target: "/opt/services/${{ secrets.PROJECT_NAME }}/infra"
    - name: Deploy
      uses: appleboy/ssh-action@v1
      with:
        envs: DOTENV_B64
        script: |
          mkdir -p /opt/services/${{ secrets.PROJECT_NAME }}/infra
          printf '%s' "$DOTENV_B64" | base64 -d > /opt/services/${{ secrets.PROJECT_NAME }}/.env
          ufw allow ${{ secrets.DEPLOY_PORT }}/tcp
          cd /opt/services/${{ secrets.PROJECT_NAME }}/infra
          docker compose --env-file ../.env -f compose.base.yml -f compose.prod.yml pull
          docker compose --env-file ../.env -f compose.base.yml -f compose.prod.yml up -d --remove-orphans
          sleep 15
          docker compose -f compose.base.yml -f compose.prod.yml ps --format json | python3 -c "
          import sys, json
          containers = json.loads(sys.stdin.read())
          if not isinstance(containers, list): containers = [containers]
          failed = [c['Name'] for c in containers if c.get('State') != 'running']
          if failed: print(f'FAILED: {failed}'); sys.exit(1)
          print(f'All {len(containers)} containers running')
          "
      env:
        DOTENV_B64: ${{ secrets.DOTENV }}
```

**Что убирается**: Ansible deploy playbook, infra-service (для деплоя), Redis deploy queue, `delegate_ansible_deploy`, DeployerNode в текущем виде.

**Ansible остаётся только для провижинга серверов** (Docker, firewall, users) — это `prod_infra`, не оркестратор.

### Workflow оркестратора для деплоя

Оркестратор не деплоит, а подготавливает:
1. Резолвит все секреты → сохраняет в БД (encrypted)
2. Собирает полный `.env` → base64 → GitHub Secret `DOTENV`
3. Пишет deploy-инфру в GitHub Secrets (`DEPLOY_HOST`, `DEPLOY_PORT`, `PROJECT_NAME`, etc.)
4. CI триггерится (push / workflow_dispatch) → деплоит
5. Оркестратор ждёт `gh wait_for_workflow` → читает результат → обновляет статус

---

## Целевой flow секретов

```
1. PO спрашивает user secrets (TELEGRAM_BOT_TOKEN и т.д.) → БД
2. Scaffolding → .env.example (имена переменных, без значений)
3. Developer работает, может добавить новые env vars в .env.example
4. [Валидация]: developer добавил все env vars в .env.example?
5. Env resolver: читает .env.example, классифицирует, генерит/вычисляет/спрашивает
   - Уже заполненные в БД → пропускает
   - Infra → генерит (СОГЛАСОВАННО — postgres password один раз)
   - Computed → вычисляет из контекста проекта
   - User → спрашивает через PO → Telegram
6. [Валидация]: каждый ключ из .env.example имеет значение в БД?
7. Allocator: сервер + порт → БД
8. Оркестратор: собирает полный .env из БД → DOTENV → GitHub Secret
9. CI: deploy workflow
10. Оркестратор: ждёт результат → обновляет статус
```

---

## Открытые вопросы

### ~~1. Env resolver~~ → решено

**Архитектура**: детерминированный pipeline, LLM только для классификации неизвестных.

**Три уровня**:
1. **Группы связанных переменных** (PostgresGroup, RedisGroup и т.д.) — генерят согласованный набор. Например PostgresGroup генерит password один раз и прокидывает в DATABASE_URL, ASYNC_DATABASE_URL, POSTGRES_PASSWORD.
2. **Паттерны** (`*_SECRET` → random, `TELEGRAM_*` → user, `APP_NAME` → computed) — как сейчас, но без генерации значений, только классификация.
3. **LLM fallback** для неизвестных — один вызов на все неизвестные переменные разом.

**LLM не генерирует значения**, а выбирает стратегию из конечного списка:
- `random_token(length)`, `random_uuid`, `random_password(length)`
- `static_value("production")`, `from_context(field)`
- `ask_po(hint)` — для user secrets

**Контекст для LLM** (собирается автоматически до вызова):
1. Комментарии из `.env.example` (строка над переменной)
2. Compose файлы — `environment:` секции (GitHub API fetch)
3. Code search — `os.getenv("VAR_NAME")` через GitHub Search API (один call на переменную)

Всё через GitHub API, код на диск не клонируется.

### ~~2. Feature deploy flow~~ → решено
### ~~3. CI trigger~~ → решено

**Два отдельных workflow**:
- `ci.yml` — on push: lint → test → build images → push to self-hosted registry с тегами `${{ github.sha }}` + `latest` (автоматический)
- `deploy.yml` — on workflow_dispatch: scp compose → write .env → pull from registry → up (только по команде оркестратора)

**Flow для feature deploy**:
```
Developer push → ci.yml (автоматически) → green →
  Оркестратор:
    fetch .env.example → сравнить keys с БД →
    new vars? → env resolver (только для новых) →
    update DOTENV в GitHub Secrets →
    gh workflow run deploy.yml →
    wait for completion → update status
```

Обнаружение новых переменных: `keys_in_example - keys_in_db = new_vars`. Не diff, а сравнение с source of truth.

Деплой **только** через `workflow_dispatch` — оркестратор единственный кто его тригерит. Это даёт ему время проверить и обновить env между build и deploy.

### ~~4. Health check~~ → решено

SSH в deploy.yml: `sleep 15` → `docker compose ps --format json` → если есть контейнер не в `running` → workflow fails. Оркестратор видит failed workflow → ставит `status: error`. MVP достаточно.

### ~~5. Docker profiles~~ → решено

Профили — dev-удобство (не стартовать tg_bot без `--profile tg`). В проде всё должно стартовать всегда. Решение: профили только в `compose.dev.yml`, не в `compose.base.yml`. Тогда `compose.base.yml + compose.prod.yml` поднимает всё без `--profile`. Deploy workflow не нужно знать про профили.

**Доработка в service-template** (не в оркестраторе): `framework/lib/compose_blocks.py` — `_render_profiles()` / `_apply_placeholders()` ставят профили только в dev overlay.

### ~~6. `main.yml.jinja` в service-template~~ → решено

**Доработка в service-template**: разделить `main.yml.jinja` на два шаблона:
- `ci.yml.jinja` — on push: lint → test → build images → push to registry (теги: `$SHA` + `latest`)
- `deploy.yml.jinja` — on workflow_dispatch: scp compose → write DOTENV → pull → up → health check

`deploy.yml` универсальный, не перечисляет env-переменные поимённо (DOTENV трюк).

### 7. Multi-project сервер (отложено)

- Resource limits (docker `mem_limit`, `cpus`) — не настроены
- Мониторинг — `health_checker.py` это заглушка
- Один проект может сожрать всю RAM и уронить соседей
- Allocator есть, но нет runtime-проверки ресурсов

### ~~8. Rollback~~ → решено (MVP)

**MVP**: возможность ручного отката, не автоматика.
1. `ci.yml` пушит образы с тегом `${{ github.sha }}` (помимо `latest`)
2. Записывать `deployed_sha` (git commit SHA) в `service_deployments` при каждом деплое
3. В deploy.yml: `cp .env .env.bak` перед перезаписью
4. Ручной rollback: оркестратор тригерит deploy.yml с конкретным SHA тегом вместо `latest`

Автооткат и откат миграций БД — за пределами MVP.

---

## Текущее состояние (2026-02-17)

Все 9 итераций реализованы. Pipeline прошёл два E2E-теста (`reverse-bot`, `reverse-message-bot`), каждый из которых выявил и исправил баги. Подробности:
- [E2E cascade failure post-mortem](../investigations/e2e-reverse-bot-cascade-failure.md) (iter 8)
- [E2E registry & CI bugs post-mortem](../investigations/e2e-reverse-message-bot-registry-and-ci-bugs.md) (iter 9)

Ключевые решения из E2E:
- GHCR заменён на self-hosted Docker Registry + Caddy TLS (GHCR не работает с GitHub App tokens)
- Registry secrets устанавливаются scaffolder'ом до первого push (а не DeployerNode после CI)
- PO consumer дропает `progress` события, включает event type в формат сообщения
