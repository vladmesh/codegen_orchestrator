---
name: e2e-check
description: Check prerequisites for Line 2 E2E tests. Verifies stack health, services, and optionally scaffold test.
allowed-tools: Bash, Read
argument-hint: "[--scaffold]"
---

# E2E Prerequisites Check

Run all prerequisite checks for Line 2 engineering E2E tests.
Report a clear GO / NO-GO verdict at the end.

If `$ARGUMENTS` contains `--scaffold`, also run the scaffold E2E test (`make test-e2e-scaffold`).

## Checks (run all, don't stop on first failure)

### 1. Docker Compose services are up

```bash
docker compose ps --format "{{.Name}} {{.Status}}"
```

All services must show "Up". Pay special attention to: `api`, `langgraph`, `engineering-worker`, `worker-manager`, `redis`, `postgres`.

If any critical service is down, report it but continue checking.

### 2. API health

```bash
curl -sf http://localhost:8000/health | jq .
```

Expected: `{"status":"ok"}`. If this fails, everything downstream will fail — mark as BLOCKER.

### 3. Engineering worker consuming

```bash
docker compose logs engineering-worker --tail=10 2>&1
```

Should show the worker is alive and listening. No crash loops or repeated errors.

### 4. Worker-manager running

```bash
docker compose logs worker-manager --tail=10 2>&1
```

Should show heartbeat or ready messages. No crash loops.

### 5. Redis connectivity

```bash
docker compose exec -T redis redis-cli ping
```

Expected: `PONG`.

### 6. (Optional) Scaffold test

Only if `--scaffold` was passed:

```bash
make test-e2e-scaffold
```

This validates copier + make setup + git push inside a worker container. Takes ~2-3 minutes.

## Output Format

After all checks, print a summary table:

```
## E2E Prerequisites Report

| Check               | Status | Notes              |
|---------------------|--------|--------------------|
| Docker services     | OK/FAIL | ...               |
| API health          | OK/FAIL | ...               |
| Engineering worker  | OK/FAIL | ...               |
| Worker-manager      | OK/FAIL | ...               |
| Redis               | OK/FAIL | ...               |
| Scaffold test       | OK/FAIL/SKIP | ...          |

**Verdict: GO / NO-GO**
```

If NO-GO, list specific blockers and suggest fixes.
