---
name: deploy
description: Deploy projects to production servers using the orchestrator
---

# Deploy Skill

You help users deploy their projects to production.

## Available Commands

### Check Deployment Readiness
```bash
orchestrator deploy check <project_id>
```
Returns: missing secrets, server allocation status, build status.

### Trigger Deployment
```bash
orchestrator deploy trigger <project_id>
```
Starts deployment pipeline. Returns task_id for tracking.

### Get Deployment Status
```bash
orchestrator deploy status <task_id>
```
Returns: current step, logs, errors.

### View Deployment Logs
```bash
orchestrator deploy logs <project_id> [--lines=100]
```

## Workflow

1. First check readiness with `deploy check`
2. If missing user secrets, ask user to provide them
3. Trigger deployment with `deploy trigger`
4. Monitor with `deploy status` until complete
5. Report deployed URL to user

## Common Issues

- **Missing secrets**: Ask user for values, don't generate
- **Port conflict**: Use `orchestrator infra allocate-port`
- **Build failure**: Check logs, may need engineering fix
