---
name: diagnose
description: Troubleshoot issues, view logs and health
---

# Diagnose Skill

You help users troubleshoot issues with their services or deployments.

## Available Commands

### View Service Logs
```bash
orchestrator diagnose logs <service_name> [--lines=100]
```
View logs for a specific service (e.g. 'web', 'worker', 'db').

### Check System Health
```bash
orchestrator diagnose health
```
Checks overall system health status.

### incidents
```bash
orchestrator diagnose incidents
```
List active incidents or errors.

## Workflow

1. If something is broken, start with `diagnose health`.
2. Drill down into specific services with `diagnose logs`.
