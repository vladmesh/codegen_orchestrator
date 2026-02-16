# Параллельные Workers

Для кодогенерации используются изолированные Docker-контейнеры с AI coding agents.

## Текущая архитектура

```
┌─────────────────────────────────────────────────────┐
│                 LangGraph Orchestrator              │
│          (Developer node в Engineering)             │
└─────────────────────────────────────────────────────┘
                         │
                  Redis streams
          (worker:commands / worker:{id}:*)
                         │
                         ▼
┌─────────────────────────────────────────────────────┐
│              worker-manager Service                 │
│    (слушает worker:commands Redis stream)           │
└─────────────────────────────────────────────────────┘
                         │
                  Docker API
                         │
                         ▼
┌─────────────────────────────────────────────────────┐
│        Worker Container (ephemeral)                 │
│  - Базовый образ: worker-base-claude / droid       │
│  - worker-wrapper: entrypoint + session mgmt       │
│  - git clone репозитория                           │
│  - Выполняет coding task                           │
│  - git commit + git push                           │
└─────────────────────────────────────────────────────┘
                         │
                  Redis streams
                         │
                         ▼
               worker:{id}:output (Developer workers only)
```

## Docker-in-Docker с Sysbox

Для запуска `docker compose` внутри контейнера используем [Sysbox](https://github.com/nestybox/sysbox) — безопасный Docker-in-Docker без privileged mode.

**Установка на хост:**
```bash
wget https://downloads.nestybox.com/sysbox/releases/v0.6.4/sysbox-ce_0.6.4-0.linux_amd64.deb
sudo dpkg -i sysbox-ce_0.6.4-0.linux_amd64.deb
```

**Внутри контейнера доступно:**
- Полноценный Docker daemon (при Docker capability)
- `git clone`, `git push`
- `docker compose up -d`
- Factory.ai Droid CLI или Claude Code CLI

## Worker Образы

Worker-base образы находятся в `services/worker-manager/`:
- `worker-base-claude` — образ с Claude Code CLI
- `worker-base-droid` — образ с Factory.ai Droid

Основные характеристики:
- Ubuntu 24.04 + Python 3.12 + Node.js
- Non-root user `worker` (uid 1000)
- Pre-installed `orchestrator-cli` + `worker-wrapper`
- Динамическая настройка через ENV (`AGENT_TYPE`)
- Hash-based image caching в worker-manager

## Ограничения

| Аспект | Ограничение |
|--------|-------------|
| RAM | ~2-4GB на worker (Docker daemon + контейнеры) |
| Startup | Docker daemon стартует 5-10 сек |
| Disk | Образы качаются в каждый worker (кэшировать через volumes) |
| GitHub API | Rate limits — добавить throttling |
