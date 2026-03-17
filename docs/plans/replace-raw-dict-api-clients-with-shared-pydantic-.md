# Replace raw dict API clients with shared Pydantic DTOs

> [!WARNING]
> Этот файл автогенерируется командой `make sync`. Не редактируйте вручную — изменения будут перезаписаны.

## Context

All API clients (langgraph 38 methods, scheduler 29 methods, infra-service 3, telegram_bot 2, scaffolder 2) return raw `dict` from `resp.json()`. This means callers use `["field"]` / `.get("field")` access patterns — no IDE autocompletion, no validation, silent breakage when API response shapes change.

Shared DTOs already exist in `shared/contracts/dto/` but most only contain enums (TaskStatus, StoryStatus, etc.) — not full response/request Pydantic models. Only ProjectDTO, ServerDTO, RunDTO, AllocationDTO, UserDTO, APIKeyDTO, AgentConfigDTO, ServiceDeploymentDTO have response models. Scheduler has already been partially migrated for Project+Server entities — this is the reference pattern.

**Scope**: ~74 dict-returning methods across 5 API clients, ~160+ caller sites using dict access. This is a multi-step migration — start with the highest-value entities (task, story, repository, application, incident) then migrate callers service-by-service.

## Steps

1. [ ] Add response+request DTOs for Task entity
   - **Input**: `shared/contracts/dto/task.py` (currently enums only), `services/api/src/schemas/task.py` (reference)
   - **Output**: `TaskDTO`, `TaskCreate`, `TaskUpdate`, `TaskEventDTO`, `TaskEventCreate` added to `shared/contracts/dto/task.py`
   - **Test**: Unit test validating TaskDTO parses a sample API response dict, TaskCreate serializes correctly

2. [ ] Add response+request DTOs for Story entity
   - **Input**: `shared/contracts/dto/story.py` (currently enums only), `services/api/src/schemas/story.py`
   - **Output**: `StoryDTO`, `StoryCreate`, `StoryUpdate` added to `shared/contracts/dto/story.py`
   - **Test**: Unit test validating StoryDTO parses a sample API response dict

3. [ ] Add response+request DTOs for Repository, Application, Incident entities
   - **Input**: `shared/contracts/dto/repository.py`, `application.py`, + new `incident.py`
   - **Output**: `RepositoryDTO`, `RepositoryCreate`, `RepositoryUpdate`, `ApplicationDTO`, `ApplicationCreate`, `ApplicationUpdate`, `IncidentDTO`, `IncidentCreate`, `IncidentUpdate` + move IncidentStatus/IncidentType enums from `shared/models/incident.py` to DTO
   - **Test**: Unit tests for each DTO parsing sample responses
   - ⚠️ needs-approval (new file `incident.py` in shared/contracts/dto/, moving enums from models)

4. [ ] Migrate Scheduler API client to use DTOs
   - **Input**: `services/scheduler/src/clients/api.py` (already partially migrated for Project+Server)
   - **Output**: All 29 dict-returning methods migrated to return typed DTOs. Task/Story/Repository/Application/Incident methods use new DTOs. Runs already have RunDTO.
   - **Test**: Update `services/scheduler/tests/unit/test_api_client.py` — assert return types are DTOs, not dicts

5. [ ] Migrate Scheduler caller sites
   - **Input**: All files in `services/scheduler/src/` that access API client results as dicts (~81 call sites)
   - **Output**: All `["field"]` / `.get("field")` access patterns replaced with attribute access (`.field`)
   - **Test**: Existing unit tests pass, add type-checking assertions where callers destructure responses

6. [ ] Migrate LangGraph API client to use DTOs
   - **Input**: `services/langgraph/src/clients/api.py` (38 dict methods, 0 DTOs currently)
   - **Output**: All methods return typed DTOs. Remove generic `get()`, `post()`, `patch()` public methods (replace with typed methods).
   - **Test**: Update `services/langgraph/tests/unit/test_api_client.py` + `test_architect_api_client.py`

7. [ ] Migrate LangGraph caller sites
   - **Input**: All files in `services/langgraph/src/` using API client results as dicts (~81 call sites)
   - **Output**: All dict access replaced with attribute access
   - **Test**: Existing unit tests pass with new typed access patterns

8. [ ] Migrate remaining services (scaffolder, infra-service, telegram_bot)
   - **Input**: API clients in scaffolder (6 methods), infra-service (3+6 methods), telegram_bot (2 methods) + their ~17 caller sites
   - **Output**: All methods return typed DTOs, all callers use attribute access
   - **Test**: Verify no remaining `["field"]` patterns on API responses in these services

9. [ ] Integration tests + cleanup
   - **Input**: All migrated services
   - **Output**: Integration test verifying DTO round-trip (API returns JSON → client parses to DTO → callers use attributes). Remove any dead `dict` type hints from client base methods.
   - **Test**: `make test-unit` passes for all services, `make lint` clean

