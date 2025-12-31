---
name: engineering
description: Manage code generation and engineering tasks
---

# Engineering Skill

You help users with code generation, analysis, and pulling requests.

## Available Commands

### Analyze Requirement
```bash
orchestrator engineering analyze "Requirement description"
```
Delegates complex analysis to the engineering subgraph.

### Trigger Engineering Task
```bash
orchestrator engineering trigger <project_id>
```
Triggers a background engineering task (e.g. implementation).

### Check Task Status
```bash
orchestrator engineering status <task_id>
```
Checks status of an asynchronous engineering task.

**Output includes:**
- Task status (queued, running, completed, failed)
- Project ID
- Creation timestamp
- Start/completion timestamps (if applicable)
- Error message (if failed)
- Result data (if completed): commit SHA, selected modules, test results

**Example:**
```bash
$ orchestrator engineering status eng-abc123
┌───────────────────────────────────────────────────┐
│ Task eng-abc123                                   │
├──────────┬────────────────────────────────────────┤
│ Type     │ engineering                            │
│ Status   │ completed                              │
│ Project  │ proj-xyz789                            │
│ Created  │ 2025-12-31 10:15:30                    │
│ Started  │ 2025-12-31 10:15:32                    │
│ Completed│ 2025-12-31 10:18:45                    │
└──────────┴────────────────────────────────────────┘

Result:
{
  "engineering_status": "done",
  "commit_sha": "a1b2c3d",
  "selected_modules": ["api", "database"],
  "test_results": {"passed": true}
}
```

### Monitor with Follow Mode (TODO)
```bash
orchestrator engineering status <task_id> --follow
```
Stream live progress events (not yet implemented).

### Create Pull Request
```bash
orchestrator engineering pr <project_id>
```
Creates a PR from the current changes.

## Workflow

1. For complex coding requests that require deep thought, use `engineering analyze`.
2. To start a long-running implementation, use `engineering trigger <project_id>`.
3. Monitor progress with `engineering status <task_id>`.
4. When completed, the task result will contain the commit SHA and test results.

## Task Lifecycle

1. **queued**: Task created, waiting for worker to pick it up
2. **running**: Worker is executing the engineering subgraph
3. **completed**: Successfully finished (check result for details)
4. **failed**: Error occurred (check error_message for details)
