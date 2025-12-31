---
name: project
description: Manage projects (list, create, update, details)
---

# Project Skill

You help users manage their projects.

## Available Commands

### List Projects
```bash
orchestrator project list
```
Lists all projects for the current user.

### Get Project Details
```bash
orchestrator project get <project_id>
```
Shows detailed information about a project, including repo URL, status, and config.

### Create Project
```bash
orchestrator project create --name "My Project" --description "Description"
```
Creates a new project.

### Update Project
```bash
orchestrator project update <project_id> --name "New Name" --description "New Desc"
```
Updates project metadata.

## Workflow

1. Use `project list` to see what's available.
2. Use `project get` to inspect specific project state.
3. Use `project create` when user wants to start something new.
