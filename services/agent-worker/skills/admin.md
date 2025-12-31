---
name: admin
description: Administrative operations
---

# Admin Skill

You help administrators manage the system.

## Available Commands

### List Nodes
```bash
orchestrator admin nodes
```
Lists all active nodes in the system.

### Trigger Node
```bash
orchestrator admin trigger <node_id>
```
Manually triggers a specific node.

### Clear State
```bash
orchestrator admin clear <project_id>
```
Clears state for a project, useful for resetting stuck workflows.

## Workflow

1. Use `admin nodes` to see system status.
2. Use `admin clear` to reset state if needed.
