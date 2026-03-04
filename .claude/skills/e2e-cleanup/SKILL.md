---
name: e2e-cleanup
description: Clean up after an E2E test. Deletes GitHub repo, worker containers, and DB records.
allowed-tools: Bash, Read
argument-hint: "<project_name> [--level C] [--server-ip IP]"
---

# E2E Cleanup

Clean up all resources created by an E2E test run.

**Arguments**:
- `$0` — project name (required, e.g. `todo_api`)
- `--level C` — if present, also clean up server deployment (Level C cleanup)
- `--server-ip IP` — server IP for Level C cleanup (required if `--level C`)

The GitHub org is `project-factory-organization`.

## Steps

### 1. Find project ID

```bash
# Search by name in API
curl -s http://localhost:8000/api/projects/ | jq -r '.[] | select(.name == "PROJECT_NAME") | .id'
```

If multiple matches, use the most recent one. If no match, warn but continue with GitHub/Docker cleanup.

### 2. Kill worker containers

```bash
docker ps --filter "name=dev-" -q --format "{{.Names}}" | grep "PROJECT_NAME" | xargs -r docker rm -f
```

### 3. Delete GitHub repo

```bash
gh repo delete project-factory-organization/PROJECT_NAME --yes
```

Verify deletion:
```bash
gh repo view project-factory-organization/PROJECT_NAME 2>&1 | grep -q "Not Found" && echo "Repo deleted OK"
```

### 4. Delete project from DB

Only if project ID was found in step 1:

```bash
curl -s -X DELETE http://localhost:8000/api/projects/PROJECT_ID
# Expected: 204 No Content (cascades: tasks + port allocations)
```

Verify:
```bash
curl -s http://localhost:8000/api/projects/PROJECT_ID | jq -r '.detail'
# Expected: "Project not found"
```

### 5. Level C only — clean server

Only if `--level C` and `--server-ip` provided:

```bash
# Get deployment records first
curl -s "http://localhost:8000/api/service-deployments/?project_id=PROJECT_ID" | jq .

# Remove from server
ssh root@SERVER_IP "
  cd /opt/apps/PROJECT_NAME && docker compose down --remove-orphans --volumes
  rm -rf /opt/apps/PROJECT_NAME
"

# Delete deployment records
DEPLOYMENT_IDS=$(curl -s "http://localhost:8000/api/service-deployments/?project_id=PROJECT_ID" | jq -r '.[].id')
for ID in $DEPLOYMENT_IDS; do
  curl -s -X DELETE http://localhost:8000/api/service-deployments/$ID
done
```

## Output

Print summary of what was cleaned:

```
## Cleanup Report: PROJECT_NAME

- Worker containers: removed (N) / none found
- GitHub repo: deleted / not found
- DB project record: deleted / not found
- Server deployment: cleaned / skipped (Level A/B)
```
