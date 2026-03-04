# E2E Report: weather_bot — /weather <city> returns mock data

> **Date**: 2026-03-04
> **Project**: weather_bot (project_id: `f02dff4e-f6a5-4c5a-8efc-aae4b84851ea`)
> **Task**: `eng-f5ef6c03fd37`
> **Test level**: C
> **Status**: Passed
> **Worker audit**: collected

---

## Timeline
- 11:21:05 - Project `weather_bot` created and `eng-f5ef6c03fd37` task triggered.
- 11:21:14 - Scaffold completed, engineering worker started.
- 11:31:50 - Engineering task completed successfully. Commit SHA: `78ac923087e1b33407d3f9e65aa3ec7cf2288d88`.
- 11:31:50 - Deploy task `deploy-f5ef6c03fd37` triggered automatically.
- 11:33:26 - Deploy task finished successfully. Service deployed to `http://80.209.235.229:8000`.
- 11:36:08 - Verified the health endpoint responds with `{"status":"ok"}`. Containers checked on the SSH server and all are running properly.

## Problems Found

### Problem 1: Generated controller has unused import
- **Type**: template
- **Severity**: minor
- **Backlog**: new
- **Description**: The generated controller stub imported `datetime` which was unused, and had unsorted imports. This was auto-fixed by `make format` but ideally shouldn't be generated in the first place.
- **Root cause**: The code generator templates produce imports whether they are needed or not.
- **Suggested fix**: Make the code generator produce ruff-clean output, or avoid including unused imports in stubs.

### Problem 2: No `.env.example` file at repo root
- **Type**: template
- **Severity**: minor
- **Backlog**: new
- **Description**: `CONTRIBUTING.md` says "All required environment variables must be documented in `.env.example`", but the repo only has `.env` and `infra/.env.test`.
- **Root cause**: Copier template does not generate a `.env.example`.
- **Suggested fix**: Include a `.env.example` in the base copier template.

### Problem 3: `POSTGRES_HOST=db` is docker-only
- **Type**: template
- **Severity**: minor
- **Backlog**: new
- **Description**: The default `POSTGRES_HOST=db` only works inside Docker. For local development or running migrations from the host, it needs to be overridden, but this is undocumented.
- **Root cause**: Lack of documentation in the `.env` template regarding docker hostnames.
- **Suggested fix**: Add a comment in `.env` explaining that `db` is the Docker hostname and how to override for local use.

### Problem 4: No `make tests unit` target
- **Type**: template
- **Severity**: minor
- **Backlog**: new
- **Description**: The task instructions mention `make tests unit` but the Makefile only supports `make tests` (all) or `make tests <service-name>`.
- **Root cause**: The Makefile lacks a target separating `unit` and `integration` tests.
- **Suggested fix**: Update docs to match the Makefile, or add the split targets.

### Problem 5: Missing Controller Generator TODOs
- **Type**: template
- **Severity**: minor
- **Backlog**: new
- **Description**: The stub docstring `""" """` in generated controllers is missing any `# TODO` comments specifying the intended logic.
- **Root cause**: Spec generator doesn't populate the function body with helpful TODOs.
- **Suggested fix**: Include `# TODO: implement` comments specifying the expected behavior from the spec YAML.
