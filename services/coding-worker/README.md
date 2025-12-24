# Coding Worker

Docker container for running AI coding agents (Factory.ai Droid) in isolated Sysbox environment.

## Features

- Sysbox runtime for secure Docker-in-Docker
- Factory.ai Droid CLI pre-installed
- GitHub CLI for repo operations
- Full internet access for package installation

## Usage

This container is spawned by the Architect/Developer nodes to execute coding tasks.

```bash
docker run --runtime=sysbox-runc --rm \
    -e GITHUB_TOKEN=... \
    -e FACTORY_API_KEY=... \
    -e REPO=org/repo-name \
    -e TASK_CONTENT="..." \
    coding-worker:latest
```

## Environment Variables

| Variable | Description |
|----------|-------------|
| `GITHUB_TOKEN` | GitHub App installation token |
| `FACTORY_API_KEY` | Factory.ai API key |
| `REPO` | Repository to clone (org/repo format) |
| `TASK_CONTENT` | The task description for the AI agent |
| `AGENTS_CONTENT` | Content of AGENTS.md file |
| `MODEL` | Model to use (default: claude-sonnet-4-5-20250929) |
