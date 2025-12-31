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
Checks status of an asynchronous deployment task.

**Output includes:**
- Task status (queued, running, completed, failed)
- Project ID
- Creation timestamp
- Start/completion timestamps (if applicable)
- Error message (if failed)
- Result data (if completed): deployed URL, deployment result

**Example:**
```bash
$ orchestrator deploy status deploy-xyz456
┌───────────────────────────────────────────────────┐
│ Task deploy-xyz456                                │
├──────────┬────────────────────────────────────────┤
│ Type     │ deploy                                 │
│ Status   │ completed                              │
│ Project  │ proj-abc123                            │
│ Created  │ 2025-12-31 11:20:15                    │
│ Started  │ 2025-12-31 11:20:17                    │
│ Completed│ 2025-12-31 11:22:30                    │
└──────────┴────────────────────────────────────────┘

Result:
{
  "deployed_url": "https://myapp.example.com",
  "deployment_result": {...}
}
```

### Monitor with Follow Mode (TODO)
```bash
orchestrator deploy status <task_id> --follow
```
Stream live progress events (not yet implemented).

### View Deployment Logs
```bash
orchestrator deploy logs <project_id> [--lines=100]
```

## Workflow

1. First check readiness with `deploy check`
2. If missing user secrets, ask user to provide them
3. Trigger deployment with `deploy trigger <project_id>`
4. Monitor with `deploy status <task_id>` until status is completed or failed
5. Report deployed URL to user from the task result

## Common Issues

- **Missing secrets**: Ask user for values, don't generate
- **Port conflict**: Use `orchestrator infra allocate-port`
- **Build failure**: Check logs, may need engineering fix
