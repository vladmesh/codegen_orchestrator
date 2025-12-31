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

### Check Status
```bash
orchestrator engineering status <task_id>
```
Checks status of an asynchronous engineering task.

### Create Pull Request
```bash
orchestrator engineering pr <project_id>
```
Creates a PR from the current changes.

## Workflow

1. For complex coding requests that require deep thought, use `engineering analyze`.
2. To start a long-running implementation, use `engineering trigger`.
