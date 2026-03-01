# E2E Investigation: todo_api — Timezone-aware datetime crash on task start

> **Date**: 2026-03-01
> **Project**: todo_api (project_id: `0da447fa-4ba7-4ca3-8eb2-53205c30465d`)
> **Task**: eng-3bf2d39ee28a
> **Test level**: A
> **Status**: Failed — task crashed immediately on startup before scaffold

---

## Timeline

```
13:28:39 — Project created via API (status: draft)
13:28:39 — Task eng-3bf2d39ee28a created (status: queued)
13:28:47 — Engineering message published to engineering:queue
13:28:47 — Engineering worker picks up job
13:28:47 — Worker calls PATCH /api/tasks/eng-3bf2d39ee28a with {status: "running", started_at: "2026-03-01T13:28:47.984936+00:00"}
13:28:48 — API returns 500: asyncpg.DataError — "can't subtract offset-naive and offset-aware datetimes"
13:28:48 — Worker catches HTTPStatusError, marks task as failed
13:28:48 — Project status set to "failed"
```

**Total time**: ~1 second. No scaffold, no worker container, no code generation attempted.

---

## Problems Found

### Problem 1: Timezone-aware datetime rejected by asyncpg

- **Type**: orchestrator
- **Severity**: critical (blocks ALL engineering tasks from starting)
- **Description**: The engineering worker sends `started_at` as a timezone-aware ISO string (`datetime.now(UTC).isoformat()`). The API deserializes this into a tz-aware `datetime` object. When SQLAlchemy/asyncpg tries to write it to the `started_at` column, it fails with:

  ```
  asyncpg.exceptions.DataError: invalid input for query argument $2:
  datetime.datetime(2026, 3, 1, 13, 28, 47... (can't subtract offset-naive and offset-aware datetimes)
  [SQL: UPDATE tasks SET status=$1::VARCHAR, started_at=$2::TIMESTAMP WITHOUT TIME ZONE, ...]
  ```

- **Root cause**: Mismatch between the SQLAlchemy model and the actual database schema.

  The **migration** (`001_add_tasks_table.py:34-35`) correctly defines:
  ```python
  sa.Column("started_at", sa.DateTime(timezone=True), nullable=True)
  sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True)
  ```

  But the **model** (`shared/models/task.py:60-61`) uses bare `mapped_column()`:
  ```python
  started_at: Mapped[datetime | None] = mapped_column(nullable=True)
  completed_at: Mapped[datetime | None] = mapped_column(nullable=True)
  ```

  Without explicit `DateTime(timezone=True)` in the model, SQLAlchemy generates `TIMESTAMP WITHOUT TIME ZONE` in its internal SQL — even though the actual PG column is `TIMESTAMPTZ`. asyncpg sees the mismatch and raises `DataError`.

- **Suggested fix**: Add explicit `DateTime(timezone=True)` to the model:
  ```python
  from sqlalchemy import DateTime
  started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
  completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
  ```

  No migration needed — the DB column is already correct.

### Problem 2: Same issue latent in Server model

- **Type**: orchestrator
- **Severity**: major (will crash when these fields are written with tz-aware values)
- **Description**: `shared/models/server.py` has similarly bare datetime columns:
  ```python
  last_health_check: Mapped[datetime | None] = mapped_column(DateTime)
  provisioning_started_at: Mapped[datetime | None] = mapped_column(DateTime)
  last_incident: Mapped[datetime | None] = mapped_column(DateTime)
  ```

  Unlike the tasks table, these **migrations also lack `timezone=True`**, so both the model and DB are timezone-naive. This is consistent but will still crash if any code sends tz-aware datetimes.

- **Suggested fix**: Audit all DateTime columns across models and migrations. Standardize on `DateTime(timezone=True)` everywhere and always use `datetime.now(UTC)`.

### Problem 3: No graceful handling of task-start failure

- **Type**: orchestrator
- **Severity**: minor
- **Description**: When the initial PATCH to set `status=running` fails, the worker catches the exception and marks the task as `failed`. This is correct behavior, but the error message stored is the raw HTTP error rather than the underlying `asyncpg.DataError` message, making it harder to diagnose from the task record alone.

---

## Key Files

| File | Relevance |
|------|-----------|
| `shared/models/task.py:59-61` | Model definition missing `DateTime(timezone=True)` |
| `services/api/migrations/versions/001_add_tasks_table.py:34-35` | Migration correctly uses `timezone=True` |
| `services/langgraph/src/workers/engineering_worker.py:486` | Sends `datetime.now(UTC).isoformat()` |
| `services/api/src/routers/tasks.py:184` | `setattr(task, field, value)` with tz-aware datetime |
| `shared/models/server.py:67-70` | Same pattern, potentially affected |
