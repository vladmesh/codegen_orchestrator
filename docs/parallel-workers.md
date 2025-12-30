# Параллельные Workers

Для независимых задач запускаем отдельные контейнеры с coding agents.

## Архитектура

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
│  - claude/droid  │            │  - claude/droid  │
│  - docker compose│            │  - docker compose│
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
    -e ANTHROPIC_API_KEY=... \
    coding-worker:latest
```

**Внутри контейнера доступно:**
- Полноценный Docker daemon
- `git clone`, `git push`
- `docker compose up -d`
- `gh pr create`

## Worker Dockerfile

```dockerfile
FROM nestybox/ubuntu-jammy-systemd-docker

RUN apt-get update && apt-get install -y git curl python3 python3-pip

# GitHub CLI
RUN curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
    | dd of=/usr/share/keyrings/githubcli.gpg \
    && echo "deb [signed-by=/usr/share/keyrings/githubcli.gpg] https://cli.github.com/packages stable main" \
    > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update && apt-get install -y gh

# Claude Code
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y nodejs \
    && npm install -g @anthropic-ai/claude-code

WORKDIR /workspace
```

## Запуск параллельных workers

```python
async def parallel_developer_node(state: dict) -> dict:
    """Run multiple coding tasks in parallel."""
    tasks = state["pending_tasks"]
    
    # Запускаем всех воркеров параллельно
    results = await asyncio.gather(*[
        spawn_sysbox_worker(task)
        for task in tasks
    ])
    
    return {
        "pending_prs": [parse_pr_url(r) for r in results],
        "pending_tasks": []
    }

async def spawn_sysbox_worker(task: dict) -> str:
    """Spawn Sysbox container for a task."""
    proc = await asyncio.create_subprocess_exec(
        "docker", "run", "--rm",
        "--runtime=sysbox-runc",
        "-e", f"TASK={task['description']}",
        "-e", f"REPO={task['repo']}",
        "coding-worker:latest",
        "/scripts/execute_task.sh",
        stdout=asyncio.subprocess.PIPE
    )
    stdout, _ = await proc.communicate()
    return stdout.decode()
```

## Reviewer Agent

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

## Ограничения

| Аспект | Ограничение |
|--------|-------------|
| RAM | ~2-4GB на worker (Docker daemon + контейнеры) |
| Startup | Docker daemon стартует 5-10 сек |
| Disk | Образы качаются в каждый worker (кэшировать через volumes) |
| GitHub API | Rate limits — добавить throttling |
