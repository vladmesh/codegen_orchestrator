# Параллельные Workers

Для кодогенерации используются изолированные Docker-контейнеры с AI coding agents.

## Текущая архитектура

```
┌─────────────────────────────────────────────────────┐
│                 LangGraph Orchestrator              │
│          (Developer node в Engineering)             │
└─────────────────────────────────────────────────────┘
                         │
                  Redis pub/sub
                         │
                         ▼
┌─────────────────────────────────────────────────────┐
│              Worker Spawner Service                 │
│         (слушает worker:spawn channel)              │
└─────────────────────────────────────────────────────┘
                         │
                  Docker API
                         │
                         ▼
┌─────────────────────────────────────────────────────┐
│              Coding Worker Container                │
│  - git clone репозитория                           │
│  - Записывает TASK.md + AGENTS.md                  │
│  - droid exec --skip-permissions-unsafe            │
│  - git commit + git push                           │
└─────────────────────────────────────────────────────┘
                         │
                  Redis pub/sub
                         │
                         ▼
              worker:result:{request_id}
```

## Docker-in-Docker с Sysbox

Для запуска `docker compose` внутри контейнера используем [Sysbox](https://github.com/nestybox/sysbox) — безопасный Docker-in-Docker без privileged mode.

**Установка на хост:**
```bash
wget https://downloads.nestybox.com/sysbox/releases/v0.6.4/sysbox-ce_0.6.4-0.linux_amd64.deb
sudo dpkg -i sysbox-ce_0.6.4-0.linux_amd64.deb
```

**Запуск worker контейнера:**
```bash
docker run --runtime=sysbox-runc -it --rm \
    -e GITHUB_TOKEN=... \
    -e FACTORY_API_KEY=... \
    coding-worker:latest
```

**Внутри контейнера доступно:**
- Полноценный Docker daemon
- `git clone`, `git push`
- `docker compose up -d`
- Factory.ai Droid CLI

## Coding Worker Dockerfile (актуальный)

```dockerfile
FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
    git curl python3 python3-pip jq ca-certificates gnupg make

# Docker + Docker Compose
RUN curl -fsSL https://get.docker.com | sh
RUN mkdir -p /usr/local/lib/docker/cli-plugins \
    && curl -SL "https://github.com/docker/compose/releases/latest/download/docker-compose-linux-$(uname -m)" \
       -o /usr/local/lib/docker/cli-plugins/docker-compose \
    && chmod +x /usr/local/lib/docker/cli-plugins/docker-compose

# GitHub CLI
RUN curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
    | dd of=/usr/share/keyrings/githubcli.gpg \
    && echo "deb [signed-by=/usr/share/keyrings/githubcli.gpg] https://cli.github.com/packages stable main" \
    > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update && apt-get install -y gh

# Factory.ai Droid CLI
RUN curl -fsSL https://app.factory.ai/cli | sh
ENV PATH="/root/.local/bin:$PATH"

COPY scripts/execute_task.sh /scripts/execute_task.sh
RUN chmod +x /scripts/execute_task.sh

WORKDIR /workspace
CMD ["/scripts/execute_task.sh"]
```

## Ограничения

| Аспект | Ограничение |
|--------|-------------|
| RAM | ~2-4GB на worker (Docker daemon + контейнеры) |
| Startup | Docker daemon стартует 5-10 сек |
| Disk | Образы качаются в каждый worker (кэшировать через volumes) |
| GitHub API | Rate limits — добавить throttling |

---

## 🚧 Планируется: Параллельные задачи с Reviewer

> [!NOTE]
> Следующий раздел описывает **запланированную**, но ещё не реализованную функциональность.

Для параллельной работы над несколькими задачами планируется архитектура с Review Agent:

```
┌─────────────────────────────────────────────────────┐
│                 LangGraph Orchestrator              │
│  tasks = [{scope: "frontend"}, {scope: "backend"}] │
└─────────────────────────────────────────────────────┘
                         │
         ┌───────────────┴───────────────┐
         ▼                               ▼
┌──────────────────┐            ┌──────────────────┐
│  Worker (task_1) │            │  Worker (task_2) │
│  - git clone     │            │  - git clone     │
│  - droid exec    │            │  - droid exec    │
│  - gh pr create  │            │  - gh pr create  │
└──────────────────┘            └──────────────────┘
         │                               │
         └───────────────┬───────────────┘
                         ▼
               ┌──────────────────┐
               │   Reviewer Agent  │
               │   gh pr review    │
               │   gh pr merge     │
               └──────────────────┘
```

### Reviewer Agent (не реализован)

```python
async def reviewer_node(state: dict) -> dict:
    """Review and merge PRs."""
    for pr_url in state["pending_prs"]:
        diff = subprocess.run(
            ["gh", "pr", "diff", pr_url],
            capture_output=True, text=True
        ).stdout
        
        review = await review_with_llm(diff)
        
        if review["approved"]:
            subprocess.run(["gh", "pr", "merge", pr_url, "--squash"])
        else:
            subprocess.run([
                "gh", "pr", "comment", pr_url,
                "--body", review["feedback"]
            ])
    
    return {"messages": [...]}
```
