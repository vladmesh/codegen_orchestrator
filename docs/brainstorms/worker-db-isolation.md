# Brainstorm: Изоляция БД оркестратора от воркеров

> **Дата**: 2026-03-03
> **Контекст**: E2E тест Level C показал, что агент внутри воркера может (и реально пытается) подключиться к postgres оркестратора вместо postgres проекта. Причина — DNS-коллизия: имя `db` резолвится в БД оркестратора, потому что воркер сидит на той же сети `codegen_internal`.
> **Status**: triaged (action items in backlog #22)

---

## Как сейчас

### Сетевая топология

```
codegen_internal (одна сеть на всё)
┌─────────────────────────────────────────┐
│  db (postgres оркестратора)              │
│  redis                                  │
│  api                                    │
│  langgraph, eng-worker, deploy-worker   │
│  worker-manager                         │
│  scheduler, infra-service               │
│  caddy, registry, telegram_bot          │
│                                         │
│  worker-dev-todo-api-xxx ───────────────┼──── dev_proj_xxx
│  worker-dev-echo-bot-yyy ───────────────┼──── dev_proj_yyy
└─────────────────────────────────────────┘
```

Воркер подключается к **двум** сетям:
1. `codegen_internal` — для доступа к redis, api, worker-manager
2. `dev_proj_{worker_id}` — для доступа к инфраструктуре проекта (postgres проекта, redis проекта)

### Проблема: DNS-коллизия `db`

Проект scaffolded с `POSTGRES_HOST=db` в `.env`. Сервис БД проекта тоже называется `db` в compose.

Из воркера `db` резолвится в **postgres оркестратора** (172.19.0.2 на `codegen_internal`), а не в postgres проекта (172.20.0.x на `dev_proj_*`).

### Текущий workaround: `project-db` alias

`compose_runner.py` добавляет alias `project-db` для сервиса `db` проекта на `dev_proj_*` сети. `orchestrator dev-env start-infra` патчит `.env`: `POSTGRES_HOST=db` → `POSTGRES_HOST=project-db`.

**Почему это плохо:**
- Хрупко: агент может вызвать `make migrate` до `start-infra` (`.env` ещё не пропатчен)
- Агент может "починить" `project-db` обратно на `db`, решив что это баг (E2E подтвердил)
- Молчаливая запись в чужую БД вместо громкой ошибки
- Лишний код: `_patch_db_hostname()`, alias-генерация, нестандартное имя хоста в `.env`

---

## Проблема 1: Сетевая изоляция

### Решение: отдельная сеть `codegen_worker`

```
codegen_internal              codegen_worker           dev_proj_{id}
┌──────────────────┐         ┌──────────────────┐     ┌──────────────┐
│  db (postgres)    │         │                  │     │ project-db   │
│  langgraph        │         │                  │     │ project-redis│
│  eng-worker       │         │                  │     └──────┬───────┘
│  deploy-worker    │         │                  │            │
│  scheduler        │         │                  │            │
│  infra-service    │         │                  │            │
│  caddy            │         │                  │            │
│  registry         │         │                  │            │
│  telegram_bot     │         │                  │            │
│                   │         │                  │            │
│  redis ───────────┼─────────┤ redis            │            │
│  api ─────────────┼─────────┤ api              │            │
│  worker-manager ──┼─────────┤ worker-manager   │            │
│                   │         │                  │            │
│                   │         │  worker ─────────┼────────────┘
└──────────────────┘         └──────────────────┘
```

Воркер подключается к `codegen_worker` (вместо `codegen_internal`) + `dev_proj_*`.

**Результат:**
- `db` из воркера → резолвится только на `dev_proj_*` → правильная БД проекта
- `redis`, `api`, `worker-manager` → доступны через `codegen_worker`
- Postgres оркестратора → невидим для воркера. Не DNS-хак, а физическая невозможность.

**Бонус:** `project-db` alias и `_patch_db_hostname()` больше не нужны. Убираем код, `.env` остаётся с нативным `POSTGRES_HOST=db`.

**Объём изменений:** ~6 строк в docker-compose.yml, ~2 строки в config.py, удаление ~30 строк мёртвого кода.

---

## Проблема 2: Ресурсы при параллельных воркерах

Каждый воркер поднимает свою инфраструктуру через `orchestrator dev-env start-infra`:
- Postgres контейнер (~100-200 MB RAM)
- Redis контейнер (~30 MB RAM)
- Возможно другие сервисы из compose проекта

5 параллельных воркеров = 5 postgres + 5 redis. На 16 GB машине терпимо, на 8 GB — предел.

### Подход A: Изолированные контейнеры (текущий)

Каждому проекту — свой postgres и redis в `dev_proj_*` сети.

| Плюсы | Минусы |
|-------|--------|
| Полная изоляция без усилий | ~250 MB RAM на воркер |
| Проект видит чистую БД | 5+ воркеров = 1.5+ GB только на инфру |
| Никаких credential-конфликтов | Долгий cold start (postgres init) |
| Уже работает | |

**Когда использовать:** до 3-5 параллельных воркеров. Текущая реальность.

### Подход B: Shared postgres, отдельные databases

Один postgres-контейнер для всех dev-проектов (не оркестраторский!). Каждому проекту создаётся отдельная database.

```
codegen_worker
┌──────────────────────────┐
│  dev-postgres (shared)    │   ← один контейнер
│    ├── db: todo_api_abc   │   ← database для воркера abc
│    ├── db: echo_bot_def   │   ← database для воркера def
│    └── db: weather_xyz    │
│  dev-redis (shared)       │
│  worker-abc               │
│  worker-def               │
│  worker-xyz               │
└──────────────────────────┘
```

Worker-manager при создании воркера:
1. `CREATE DATABASE project_{worker_id}`
2. `CREATE USER project_{worker_id} WITH PASSWORD '...'`
3. `GRANT ALL ON DATABASE ... TO ...`
4. Инжектит `POSTGRES_HOST=dev-postgres`, `POSTGRES_DB=project_{id}`, `POSTGRES_USER/PASSWORD` в `.env`

При удалении воркера:
1. `DROP DATABASE project_{worker_id}`
2. `DROP USER project_{worker_id}`

| Плюсы | Минусы |
|-------|--------|
| Один postgres на всех (~200 MB total) | Нужен "DB provisioner" в worker-manager |
| Быстрый старт (CREATE DATABASE vs container init) | Shared failure: postgres падает — все воркеры встают |
| Изоляция через pg credentials | Суперюзер всё равно видит все БД |
| Просто масштабируется | Нужно менять `.env` и compose проекта |

**Когда использовать:** 5+ параллельных воркеров, ограниченные ресурсы.

### Подход C: Shared postgres, разные schemas

Как B, но вместо отдельных databases — schemas в одной database.

| Плюсы | Минусы |
|-------|--------|
| Ещё меньше overhead | Слабая изоляция (один user = доступ ко всем schemas) |
| Одно подключение | Сложнее cleanup |
| | Ломает проекты которые рассчитывают на `public` schema |

**Вердикт:** Слишком хрупко. Если делать shared — лучше отдельные databases (подход B).

### Подход D: Гибрид — shared по умолчанию, изолированный по запросу

Worker-manager решает на основе конфига проекта:
- `backend` only → shared postgres (подход B)
- Сложный проект с кастомными extensions / несколькими БД → свой контейнер (подход A)

Нужен флаг в конфиге проекта или автодетект по `compose.base.yml`.

---

## Проблема 3: Масштабирование — где живут воркеры?

> **Контекст**: Подходы A-D выше решают ресурсную проблему на одном хосте. Но настоящий вопрос — как масштабироваться до десятков и сотен параллельных воркеров? Нужно думать шире: выход за пределы одной машины.

### Что нужно каждому воркеру

1. Docker runtime (запуск контейнеров проекта: postgres, redis, app)
2. Workspace на диске (~500 MB - 2 GB: repo + node_modules / venv)
3. Доступ к оркестратору (Redis streams для команд, API для состояния)
4. Изоляция от других воркеров (сеть, файловая система, DNS)
5. Возможность запускать `make migrate`, `make test`, `docker compose up`

### Вариант E: Отдельный "worker farm" сервер

Оркестратор на одной машине, воркеры — на выделенном сервере (или нескольких).

```
Orchestrator host                    Worker farm (Hetzner AX52, 64GB)
┌──────────────────┐                ┌──────────────────────────────┐
│  api, langgraph  │                │  worker-abc + dev_proj_abc   │
│  redis, db       │◄──WireGuard──►│  worker-def + dev_proj_def   │
│  telegram_bot    │                │  worker-xyz + dev_proj_xyz   │
│  worker-manager  │──Docker SSH──►│  ...30-50 воркеров...        │
│  caddy, registry │                │                              │
└──────────────────┘                └──────────────────────────────┘
```

Worker-manager управляет удалённым Docker через `DOCKER_HOST=ssh://worker-farm`.

| Плюсы | Минусы |
|-------|--------|
| Минимум архитектурных изменений | Нужен VPN (WireGuard) между хостами |
| DNS-коллизия невозможна (разные Docker daemons) | Docker API по SSH — чуть медленнее |
| Воркер не может убить оркестратор OOM | Один сервер = single point of failure |
| Hetzner AX52: 64 GB RAM, €62/мес → 30-50 воркеров | Нужен мониторинг ресурсов на ферме |
| Масштабирование: добавить ещё сервер | Worker-manager должен знать про несколько хостов |

**Масштаб:** ~30-50 воркеров на сервер. 2-3 сервера = 100+ воркеров.

**Worker-manager изменения:** конфиг `WORKER_DOCKER_HOSTS=ssh://farm1,ssh://farm2`, round-robin или по свободной RAM.

### Вариант F: Cloud VMs on-demand (Hetzner Cloud / DigitalOcean)

Каждый воркер — отдельная cloud VM. Создаётся по запросу, уничтожается после завершения.

```
Orchestrator host                    Hetzner Cloud
┌──────────────────┐                ┌─────────────┐
│  api, langgraph  │                │ VM: CX22    │ ← worker-abc
│  redis, db       │◄──public IP──►│ 4GB, docker │
│  worker-manager  │                └─────────────┘
│  (creates VMs    │                ┌─────────────┐
│   via API)       │                │ VM: CX22    │ ← worker-def
└──────────────────┘                └─────────────┘
```

Worker-manager → Hetzner Cloud API → создаёт VM → cloud-init ставит Docker → запускает worker agent.

| Плюсы | Минусы |
|-------|--------|
| Полная изоляция (разные машины, разные IP) | Spinup 30-60 сек (cloud-init + docker pull) |
| Платишь только за время работы | Hetzner CX22: €0.007/час, CX32: €0.014/час |
| Бесконечный масштаб | Нужен надёжный provisioner (cloud-init, API) |
| VM умирает — никакого cleanup | Redis/API оркестратора должны быть доступны извне |
| Нет ресурсных ограничений на общем хосте | Стоимость при десятках воркеров 24/7 |

**Стоимость:** 10 воркеров × 4 часа × €0.007 = €0.28/день. 50 воркеров × 4 часа = €1.40/день = ~€42/мес. Сравнимо с dedicated сервером, но с полной изоляцией и elastic scaling.

**Оптимизация — pre-warm pool:** Держать 2-3 готовых VM в "спящем" режиме. Spinup из пула: ~5 сек вместо 60.

### Вариант G: Fly.io Machines (managed microVMs)

Fly Machines — Firecracker microVMs с Docker-совместимым API. По сути managed версия варианта H.

| Плюсы | Минусы |
|-------|--------|
| Создание machine: ~300ms из образа | Vendor lock-in |
| Оплата: ~$0.19/day за shared CPU | Нет Docker-in-Docker (postgres = отдельная machine) |
| Каждая machine — изолированная microVM | Нужно адаптировать worker архитектуру |
| Persistent volumes: $0.15/GB/мес | Network latency до оркестратора |
| REST API для lifecycle management | Непрозрачное ценообразование при масштабе |

**Проблема**: worker сейчас запускает `docker compose up` внутри себя. На Fly это невозможно. Нужно перестроить: postgres-as-a-service (Fly Postgres или Neon) + worker machine без Docker-in-Docker.

**Вердикт:** Интересно если готовы переосмыслить архитектуру воркера. Не подходит как drop-in замена.

### Вариант H: Self-hosted Firecracker / Kata Containers

MicroVM на своём железе. Firecracker — то на чём работает AWS Lambda. Boot за <125ms, ~5 MB overhead.

- **Firecracker напрямую**: каждый воркер — microVM с полным Linux. Внутри — Docker daemon, postgres, всё. Изоляция на уровне ядра.
- **Kata Containers**: обёртка — `docker run` создаёт microVM вместо контейнера. Прозрачно для worker-manager.

| Плюсы | Минусы |
|-------|--------|
| Изоляция уровня VM, скорость уровня контейнера | Сложный setup (особенно Firecracker напрямую) |
| Boot <125ms, ~5 MB RAM overhead | Docker-in-VM требует nested virtualization |
| Безопасность: `rm -rf /` убивает только microVM | Kata Containers менее зрелый на не-Cloud платформах |
| Идеально для untrusted workloads | Нужен bare-metal (KVM), не работает в VM-хостинге |

**Вердикт:** Отличная технология, но слишком сложная для текущего этапа. Имеет смысл когда безопасность воркеров станет критичной (публичные пользователи).

### Вариант I: Kubernetes (namespaces per worker)

Namespace per worker, pod-level isolation, network policies.

```
K8s cluster (3 nodes, Hetzner €30/мес)
├── namespace: orchestrator
│   ├── pod: api
│   ├── pod: langgraph
│   ├── pod: redis, postgres
│   └── ...
├── namespace: worker-abc
│   ├── pod: worker-agent
│   ├── pod: postgres
│   └── pod: redis
├── namespace: worker-def
│   └── ...
```

| Плюсы | Минусы |
|-------|--------|
| Resource limits per namespace (CPU, RAM) | Огромная сложность для маленькой команды |
| Network policies: namespace A ≠ namespace B | K8s — отдельный проект на поддержку |
| Auto-scaling (HPA, cluster autoscaler) | Docker-in-K8s: нужен kaniko или dind sidecar |
| Managed K8s: Hetzner, DO, etc. | Переписывание worker-manager → K8s operator |

**Вердикт:** Overkill на текущем масштабе. Имеет смысл при 100+ воркерах и нескольких людях в команде.

### Вариант J: Docker Swarm / Nomad

Проще чем K8s, мощнее чем один Docker host.

- **Docker Swarm**: нативный Docker, multi-node, service placement. `docker swarm join` на новом сервере и готово.
- **Nomad**: HashiCorp, поддерживает Docker + raw_exec + QEMU. Легковесный.

| Плюсы | Минусы |
|-------|--------|
| Swarm: zero config, нативный Docker | Swarm: фактически заброшен Docker Inc |
| Nomad: лёгкий, один бинарник | Nomad: меньше community, меньше интеграций |
| Multi-node из коробки | Сетевая изоляция менее зрелая чем в K8s |
| Worker-manager → Swarm service create | Ограниченный auto-scaling |

**Вердикт:** Swarm — рискованно из-за неясного будущего. Nomad — интересен как middle ground между "руками" и K8s.

---

## Сравнительная таблица всех вариантов

| Вариант | Spinup | Изоляция | Стоимость (10 workers) | Сложность | Масштаб |
|---------|--------|----------|------------------------|-----------|---------|
| Текущий (один хост) | мгновенно | слабая (Docker network) | €0 (уже есть) | низкая | ~5-10 |
| **E**: Worker farm (dedicated) | мгновенно | средняя (Docker) | ~€62/мес | низкая | ~30-50/сервер |
| **F**: Cloud VMs on-demand | 30-60s | полная (разные машины) | ~€1-3/день | средняя | ∞ |
| **G**: Fly.io Machines | <1s | полная (microVM) | ~$2/день | средняя | ∞ |
| **H**: Firecracker/Kata | <1s | полная (VM) | ~€62/мес (self-hosted) | высокая | ~50+/сервер |
| **I**: Kubernetes | 5-10s | средняя+ (namespaces) | ~€80+/мес | высокая | ∞ |
| **J**: Docker Swarm/Nomad | 2-5s | средняя | ~€62-120/мес | средняя | ~100+ |

---

## Рекомендация: эволюционный путь

### Phase 1 — Сетевая изоляция ✅ (done 2026-03-03, #22)

`codegen_worker` network. Закрыли DNS-коллизию, удалили `project-db` alias и `_patch_db_hostname()` workaround.

### Phase 2 — Worker farm (5-10 параллельных воркеров)

Выносим воркеров на отдельный Hetzner dedicated сервер. Worker-manager работает с remote Docker через SSH. Оркестратор и ферма связаны через WireGuard VPN.

**Что меняется:**
- `worker-manager`: конфиг `WORKER_DOCKER_HOSTS`, выбор хоста при создании воркера
- Инфра: WireGuard туннель, Redis/API слушают на VPN-интерфейсе
- Monitoring: ресурсы фермы (RAM, disk, CPU)

**Что не меняется:** архитектура воркера, compose, tooling.

### Phase 3 — Multi-farm + shared postgres (10-50 воркеров)

Несколько worker-farm серверов. Round-robin или by-free-RAM балансировка. Shared postgres per farm (подход B) для экономии RAM.

**Что меняется:**
- Worker-manager: multi-host placement strategy
- DB provisioner: `CREATE DATABASE` / `DROP DATABASE` при создании/удалении воркера
- Credential injection: per-worker postgres user/password

### Phase 4 — Elastic scaling (50+ воркеров)

Cloud VMs on-demand (Hetzner Cloud API, вариант F) или Fly Machines (вариант G). Pre-warm pool для быстрого старта. Pay-per-use.

К этому моменту уже понятно:
- Средняя стоимость одного воркера (compute-часы)
- Среднее время жизни воркера
- Пиковая нагрузка (сколько параллельных)
- Окупается ли elastic vs dedicated

**Решение Phase 4 принимается на основе данных Phase 3.**

---

## Открытые вопросы

1. Нужен ли shared Redis для dev-проектов? Большинство backend-only проектов redis не используют. Те что используют — обычно для кеша, можно шарить с prefix isolation (`REDIS_KEY_PREFIX=project_{id}`).
2. Как обрабатывать кастомные compose-сервисы? Если проект определяет `elasticsearch` или `rabbitmq` в compose — они всегда поднимаются как изолированные контейнеры на `dev_proj_*`.
3. Health monitoring для shared postgres? Если он падает — все воркеры зависают. Нужен ли автоматический рестарт или алерт?
4. Worker-manager и remote Docker: как мониторить здоровье воркеров на удалённых хостах? Docker events stream через SSH? Отдельный agent на ферме?
5. Безопасность: при выходе на multi-host — WireGuard достаточно? Нужна ли mTLS между воркерами и оркестратором?
6. Disk I/O: на worker farm N воркеров пишут в один SSD. NVMe справится с 30-50 параллельными postgres? Нужен ли tmpfs для тестовых БД?
