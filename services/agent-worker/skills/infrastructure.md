---
name: infrastructure
description: Manage servers and resource allocations
---

# Infrastructure Skill

You help users manage infrastructure resources like servers and ports.

## Available Commands

### List Infrastructure
```bash
orchestrator infra list
```
Lists assigned servers and resource allocations.

### Allocate Port
```bash
orchestrator infra allocate-port <project_id>
```
Allocates a new port for a project if needed.

### Release Resource
```bash
orchestrator infra release <allocation_id>
```
Releases a resource allocation.

## Workflow

1. Check `infra list` to see current usage.
2. If a deploy fails due to port conflict, use `infra allocate-port`.
