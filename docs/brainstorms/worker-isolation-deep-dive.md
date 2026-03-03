# Worker Isolation: Deep Dive по архитектурным вариантам

> **Контекст:** Нужна изоляция десятков→сотен воркеров, каждый из которых поднимает стек проекта (postgres, redis, app), запускает миграции, тыкает в БД. Воркеры не должны конфликтовать ни друг с другом, ни с оркестратором.

---

## Что нам реально нужно (требования)

| Требование | Приоритет | Комментарий |
|---|---|---|
| **Полная изоляция network/DNS** | 🔴 Must | Воркер не видит чужие DB/redis |
| **Собственный postgres + redis** | 🔴 Must | Миграции, seed data, extensions |
| **docker compose** внутри воркера | 🔴 Must | `make start-infra`, `make migrate` |
| **Масштабирование до 100+ воркеров** | 🟡 Want | Не сегодня, но архитектура должна позволять |
| **Доступ к redis/api оркестратора** | 🔴 Must | Отчёты, получение задач, статусы |
| **Быстрый cold start** | 🟡 Want | <30s до готовности к работе |
| **Стоимость ~пропорциональна нагрузке** | 🟡 Want | Не платить за idle |
| **Простота отладки** | 🟡 Want | docker logs, exec into container |

---

## Вариант 1: Docker networking (текущий вектор)

> Всё на одном сервере. Изоляция через Docker-сети.

### 1A: Отдельная сеть `codegen_worker` (из brainstorm)

```
codegen_internal     codegen_worker       dev_proj_{id}
┌────────────┐      ┌──────────────┐     ┌──────────────┐
│ db (orch)  │      │              │     │ db (project)  │
│ langgraph  │      │              │     │ redis (proj)  │
│ deploy-wkr │      │              │     └───────┬───────┘
│ scheduler  │      │              │             │
│ caddy      │      │              │             │
│            │      │              │             │
│ redis ─────┼──────┤ redis        │             │
│ api ───────┼──────┤ api          │             │
│ wkr-mgr ───┼──────┤ worker-mgr   │             │
│            │      │   worker ────┼─────────────┘
└────────────┘      └──────────────┘
```

| | |
|---|---|
| 👍 Минимум изменений (~40 строк) | 👎 Один сервер = потолок RAM/CPU |
| 👍 Физическая невозможность DNS-коллизии | 👎 ~250MB RAM на воркер (postgres+redis) |
| 👍 Удаление хрупкого workaround | 👎 100 воркеров = 25GB только на инфру |
| 👍 Уже почти работает | 👎 Docker daemon — single point of failure |

**Потолок:** ~20-30 воркеров на 64GB сервере.

### 1B: Docker-in-Docker (полная изоляция)

Каждый воркер — DinD контейнер со своим Docker daemon. Проектный compose работает **внутри** воркера.

```
Host Docker
├── orchestrator stack (codegen_internal)
├── worker-1 (DinD) ─── внутренний docker daemon
│   └── project stack (postgres, redis, app)
├── worker-2 (DinD) ─── внутренний docker daemon
│   └── project stack
└── ...
```

| | |
|---|---|
| 👍 Абсолютная изоляция — отдельный Docker daemon | 👎 DinD = privileged mode (security risk) |
| 👍 Воркер не видит хост-docker вообще | 👎 Двойной overhead: nested containers |
| 👍 `docker compose` внутри "just works" | 👎 Сложнее отладка (`docker exec` в DinD → `docker exec` внутри) |
| 👍 Можно ограничить cgroups на уровне воркера | 👎 Медленнее: nested filesystem layers |

**Потолок:** ~15-20 воркеров на 64GB (больше overhead на DinD daemon).

---

## Вариант 2: VM-based изоляция

### 2A: Lightweight VMs (Firecracker / Cloud Hypervisor)

Firecracker (от AWS Lambda) — micro-VM, стартует за 125ms, ~5MB overhead.

```
Host
├── orchestrator (docker compose)
├── firecracker-vm-1 (~256MB RAM)
│   ├── mini Linux
│   ├── docker daemon
│   └── project stack
├── firecracker-vm-2
└── ...
```

| | |
|---|---|
| 👍 Настоящая изоляция (hypervisor boundary) | 👎 Нужен bare-metal или KVM-enabled VPS |
| 👍 Быстрый старт (~1-2s для VM) | 👎 Сложная оркестрация (Firecracker API ≠ Docker API) |
| 👍 Жёсткие лимиты RAM/CPU на уровне VM | 👎 Нужна своя система image management |
| 👍 Crash воркера не роняет хост | 👎 Networking между VM и оркестратором — нетривиально |
| 👍 Можно гранулярно выделять ресурсы | 👎 Нет готового compose-workflow внутри VM |

> [!IMPORTANT]
> Firecracker — это уровень Fly.io / AWS Lambda. Мощно, но это отдельный инфраструктурный проект. Реалистично если строить платформу для сотен клиентов.

### 2B: Классические VMs (KVM/QEMU, Proxmox)

Тяжёлые VM с полной ОС. Pre-baked образ с Docker + базовыми инструментами.

| | |
|---|---|
| 👍 Полная изоляция, security boundary | 👎 ~30-60s cold start |
| 👍 Понятная модель | 👎 ~512MB-1GB base RAM на VM |
| 👍 Можно snapshot/restore | 👎 Не масштабируется за 20-30 VM на сервере |

**Вердикт:** Слишком тяжело для dev-воркеров. Имеет смысл только если нужна security isolation (untrusted code).

---

## Вариант 3: Multi-server (горизонтальное масштабирование)

### 3A: Dedicated worker servers

Оркестратор на одном сервере, воркеры — на отдельных.

```
┌─────────────────┐          ┌─────────────────────┐
│  Orchestrator    │          │  Worker Server 1     │
│  Server          │          │  ┌─────────────────┐ │
│  ┌─────────────┐│   HTTP   │  │ worker-manager-1 │ │
│  │ api         ├┼──────────┼──┤ worker-1..N      │ │
│  │ langgraph   ││          │  │ project stacks   │ │
│  │ db, redis   ││          │  └─────────────────┘ │
│  │ worker-mgr  ││          └─────────────────────┘
│  │ (scheduler) ││          ┌─────────────────────┐
│  └─────────────┘│          │  Worker Server 2     │
└─────────────────┘          │  ...                 │
                             └─────────────────────┘
```

**Как это работает:**
1. Worker-manager на оркестраторе решает куда разместить воркер (scheduling)
2. На каждом worker-server крутится **satellite worker-manager** (принимает команды, управляет локальными контейнерами)
3. Коммуникация orchestrator ↔ worker-server через HTTP/gRPC или Redis pub/sub

| | |
|---|---|
| 👍 Горизонтальное масштабирование | 👎 Нужен scheduler (bin-packing) |
| 👍 Crash worker-server не роняет оркестратор | 👎 Сетевая латентность (worker ↔ orchestrator API) |
| 👍 Можно добавлять серверы по мере роста | 👎 Worker-manager становится distributed system |
| 👍 Полная изоляция по определению | 👎 Docker image distribution между серверами |
| 👍 Дешёвые серверы (Hetzner ~€5-10/мес за 8GB) | 👎 Мониторинг, деплой, обновления на N серверах |

> [!TIP]
> **Ключевое архитектурное изменение:** worker-manager split на **coordinator** (на оркестраторе, принимает задачи, выбирает сервер) и **agent** (на worker-server, управляет контейнерами). Коммуникация через Redis — уже есть!

### 3B: Docker Swarm

Встроенный в Docker оркестратор кластера. Один manager + N worker nodes.

```
Swarm Manager (orchestrator server)
├── orchestrator services (api, langgraph, ...)
├── worker-service (replicated, global scheduling)
│   ├── node-1: worker-1, worker-2
│   ├── node-2: worker-3, worker-4
│   └── node-3: worker-5, worker-6
```

| | |
|---|---|
| 👍 Нативный Docker — минимум нового | 👎 Docker Swarm «мёртв» (минимум развития) |
| 👍 Overlay networks из коробки | 👎 Overlay network = overhead на VXLAN |
| 👍 `docker stack deploy` | 👎 Compose v3 ограничения (нет depends_on conditions) |
| 👍 Service discovery встроен | 👎 Каждый worker всё равно нужен как отдельный контейнер, не service replica |

**Вердикт:** Плохой fit. Наши воркеры — это не stateless replicas, каждый уникален.

### 3C: Kubernetes (K3s)

K3s — lightweight Kubernetes, работает на 512MB RAM.

```
K3s cluster
├── namespace: orchestrator
│   └── deployments: api, langgraph, db, redis, ...
├── namespace: worker-abc
│   ├── pod: worker (Claude/agent)
│   ├── pod: postgres
│   └── pod: redis
├── namespace: worker-def
│   └── ...
```

| | |
|---|---|
| 👍 Namespace = natural isolation boundary | 👎 K8s learning curve |
| 👍 NetworkPolicy = fine-grained network rules | 👎 Нужно переписать compose → Helm charts |
| 👍 Resource quotas per namespace | 👎 Overhead: etcd, API server, kubelet |
| 👍 Multi-node из коробки | 👎 Worker внутри — всё ещё Docker, compose не работает |
| 👍 Auto-scaling, self-healing | 👎 Overkill для текущего масштаба |

> [!WARNING]
> **Ключевая проблема:** Внутри воркера запускается `docker compose` (через orchestrator-cli). В K8s нет Docker daemon. Нужен либо DinD sidecar, либо переписывать всю devops-логику на K8s manifests.

---

## Вариант 4: Cloud-native

### 4A: Cloud containers (ECS Fargate / Cloud Run / Fly.io)

Каждый воркер — managed container на cloud платформе.

```
Cloud Provider
├── Orchestrator (1 VM / dedicated server)
│   └── api, langgraph, db, redis
├── Fly.io Machine: worker-1 (2 CPU, 2GB)
├── Fly.io Machine: worker-2
└── ...
```

| Провайдер | vCPU | RAM | Стоимость | Фича |
|---|---|---|---|---|
| **Fly.io Machines** | 1-8 | 256MB-8GB | ~$0.007/час (1c1g) | Start/stop за секунды, оплата per-second |
| **Hetzner Cloud** | 2 | 4GB | €4.5/мес | Дёшево, но полная VM |
| **AWS Fargate** | 0.25-4 | 0.5-30GB | ~$0.04/час (1c2g) | Managed, дорого |
| **Google Cloud Run** | 1-8 | 128MB-32GB | ~$0.05/час | Serverless, auto-scale |

| | |
|---|---|
| 👍 Pay per use (stop when idle) | 👎 Latency orchestrator ↔ worker (network) |
| 👍 Infinite scale | 👎 Docker-in-Docker в managed container — сложно/невозможно |
| 👍 No server management | 👎 Стоимость при 100 воркеров 24/7: $$$ |
| 👍 Geo-distribution possible | 👎 Нужен persistent storage для workspace |

> [!CAUTION]
> **Fly.io Machines** — самый интересный вариант из cloud. Можно останавливать/запускать машину за 300ms, платить только за активное время. Но Docker-in-Docker внутри Fly Machine — нужно проверять.

### 4B: Hybrid — Orchestrator self-hosted, workers on cloud

**Самый прагматичный cloud-подход:**

```
Self-hosted (Hetzner €10/мес)          Cloud (Fly.io / Hetzner Cloud)
┌────────────────────┐                ┌─────────────────────┐
│ Orchestrator       │    Redis +     │ worker-1 (Fly.io)   │
│ api, langgraph,    │◄───HTTP────────│  docker compose      │
│ db, redis,         │                │  project stack      │
│ worker-coordinator │                └─────────────────────┘
└────────────────────┘                ┌─────────────────────┐
                                      │ worker-2 (Fly.io)   │
                                      └─────────────────────┘
```

Worker-manager (coordinator) на оркестраторе:
1. Получает задачу
2. `flyctl machine run worker-image --env ...` или HTTP API
3. Worker стартует, подключается к Redis оркестратора по external URL
4. Worker работает автономно, отчитывается через API
5. По завершении — machine stop (перестаём платить)

---

## Вариант 5: Подход GitHub Codespaces / Gitpod

> "Каждый воркер — это полноценное dev environment"

Gitpod/Codespaces под капотом используют K8s + nested containers. Можно переиспользовать подход:

1. Pre-built workspace images (кэшируем "scaffolded project + dependencies")
2. Workspace стартует из snapshot за ~5s
3. Внутри workspace — полный Linux с Docker (через Sysbox или user-namespaces)

**Sysbox** — OCI runtime, позволяет Docker-in-Docker **без privileged mode**:

```
Host (с Sysbox runtime)
├── sysbox-worker-1 (unprivileged container)
│   ├── systemd
│   ├── dockerd (nested, isolated)
│   └── project stack (postgres, redis, app)
├── sysbox-worker-2
└── ...
```

| | |
|---|---|
| 👍 Настоящий Docker внутри без privileged | 👎 Sysbox — дополнительная зависимость |
| 👍 Полная изоляция | 👎 Больше RAM overhead (~100MB на sysbox контейнер) |
| 👍 Worker "не знает" что он в контейнере | 👎 Совместимость (не все ядра поддерживают) |

---

## Сравнительная таблица

| Вариант | Изоляция | Масштаб | Сложность | Стоимость | Cold Start |
|---|---|---|---|---|---|
| **1A: Docker networks** | 🟡 DNS only | ~30 | 🟢 Минимум | 🟢 0 (есть сервер) | 🟢 ~5s |
| **1B: DinD** | 🟢 Full | ~20 | 🟡 Средне | 🟢 0 | 🟡 ~15s |
| **2A: Firecracker** | 🟢🟢 VM | ~50 | 🔴 Высоко | 🟢 0 | 🟢 ~2s |
| **3A: Multi-server** | 🟢 Full | ~100+ | 🟡 Средне | 🟡 €20-50/мес | 🟢 ~5s |
| **3C: K3s** | 🟢 Namespace | ~200+ | 🔴 Высоко | 🟡 €20-50/мес | 🟡 ~10s |
| **4B: Hybrid cloud** | 🟢 Full | ∞ | 🟡 Средне | 🟡 Pay-per-use | 🟡 ~5-10s |
| **5: Sysbox** | 🟢 Full | ~30 | 🟡 Средне | 🟢 0 | 🟢 ~5s |

---

## Моя рекомендация: Path of Least Resistance

### Фаза 1 (сейчас): Docker networks → `codegen_worker`
- Закрывает реальную проблему за день
- 0 новых зависимостей, 0 новых серверов
- Потолок ~20-30 воркеров — достаточно на ближайшие месяцы

### Фаза 2 (когда >10 параллельных воркеров): Multi-server split
- Добавить 1-2 worker-сервера (Hetzner CX22 — €4.5/мес, 4GB RAM)
- Worker-manager split: coordinator → agent (через Redis, который уже есть)
- Каждый worker-server — просто Docker host, запускает воркеры локально
- Архитектура уже готова: `WORKER_REDIS_URL`, `WORKER_API_URL` — подключение к remote оркестратору

### Фаза 3 (когда >50 воркеров): Cloud burst
- Основная нагрузка — на своих серверах (Hetzner)
- Пиковая нагрузка — Fly.io Machines (start/stop per задачу)
- Worker-coordinator выбирает: свой сервер vs cloud, по загрузке

### Если нужна security isolation (untrusted code): Sysbox
- Вместо DinD — Docker через Sysbox runtime
- Без privileged mode, с настоящей изоляцией
- Можно внедрить на любой фазе

> [!IMPORTANT]
> **Ключевой архитектурный вывод:** Самое ценное, что можно сделать сейчас — абстрагировать worker placement. Worker-manager уже принимает `network_name` как параметр. Если добавить абстракцию "где запускать воркер" (local docker / remote server / cloud), остальное — детали реализации.
