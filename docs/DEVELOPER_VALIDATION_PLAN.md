# Developer Node Validation & Rework Plan

## Problem Statement

Currently, the Developer node can complete execution without pushing changes to the repository. There's no mechanism to:
1. Verify that a commit and push actually occurred
2. Provide feedback to the developer agent for refinement
3. Handle incomplete work gracefully

This plan addresses these gaps while considering architectural constraints.

---

## Current Architecture

### Flow
```
Product Owner → Engineering Subgraph → Architect → Preparer → Developer → Tester
```

### Key Components

**Developer Node** (`services/langgraph/src/nodes/developer.py`)
- Spawns Factory.ai Droid worker via `spawn_worker()`
- Receives `commit_sha` in result but doesn't validate it
- Doesn't use own prompt template - delegates to Factory.ai

**Worker Spawner** (`services/worker-spawner/src/main.py`)
- Creates Docker containers with `--rm` flag (auto-cleanup)
- Container terminates after result is published to Redis

**Engineering Subgraph** (`services/langgraph/src/subgraphs/engineering.py`)
- Routes: `developer` → `developer_spawn_worker` → `tester`
- No validation of commit/push before proceeding to tests

---

## Proposed Changes

### 1. Update CLI Agent Prompt Template

#### Location
`scripts/cli_agent_configs.yaml`

#### Action
Add new entry for `developer.spawn_worker`:

```yaml
- id: "developer.spawn_worker"
  name: "Developer Factory Worker"
  provider: "factory"
  model_name: "claude-sonnet-4-5-20250929"
  timeout_seconds: 600
  required_credentials:
    - "GITHUB_TOKEN"
  provider_settings:
    autonomy: "high"
  prompt_template: |
    You are implementing business logic for a software project.
    
    **CRITICAL REQUIREMENT - MUST COMMIT AND PUSH:**
    
    Your work is INCOMPLETE unless you:
    1. Commit ALL your changes with a clear, descriptive commit message
    2. Push the commit to the repository
    3. The commit SHA must be captured in the logs
    
    Failure to push changes will trigger a rework cycle where you'll need to:
    - Review feedback on what was missing
    - Complete the commit and push
    - Try again
    
    **Guidelines:**
    - Write clean, well-tested code
    - Follow project conventions
    - Document significant logic
    - Ensure all changes are committed before finishing
    
    The orchestrator will verify that changes were pushed. If no commit SHA 
    is detected, your work will be considered incomplete.
```

#### Deployment
Run seeding script to update configuration:
```bash
python scripts/seed_agent_configs.py --api-base-url http://localhost:8000
```

---

### 2. Add Validation to Developer Node

#### Location
`services/langgraph/src/nodes/developer.py`

#### Changes
Update `spawn_worker()` method to validate commit SHA:

```python
async def spawn_worker(self, state: dict) -> dict:
    """Spawn Factory.ai worker to implement business logic.
    
    Validates that changes were committed and pushed.
    """
    # ... existing setup code ...
    
    worker_result = await request_spawn(...)
    
    if worker_result.success:
        # VALIDATION: Check if commit/push actually happened
        if not worker_result.commit_sha:
            logger.warning(
                "developer_no_commit_detected",
                repo_name=repo_name,
                request_id=worker_result.request_id,
            )
            return {
                "messages": [
                    AIMessage(
                        content=f"⚠️ Developer worker finished but did NOT push changes!\n\n"
                        f"The worker completed execution but no commit SHA was detected. "
                        f"This likely means changes were made locally but not committed/pushed.\n\n"
                        f"Feedback: Please commit all your changes and push to the repository."
                    )
                ],
                "worker_info": worker_result,
                "developer_needs_rework": True,
                "developer_feedback": "No commit/push detected. Please commit and push your changes.",
                "developer_iteration_count": state.get("developer_iteration_count", 0),
            }
        
        # Success with commit
        logger.info(
            "developer_worker_success",
            repo_name=repo_name,
            commit_sha=worker_result.commit_sha,
        )
        return {
            "messages": [
                AIMessage(
                    content=f"✅ Developer worker completed successfully!\n"
                    f"Commit: {worker_result.commit_sha}\n"
                    f"Branch: {worker_result.branch or 'N/A'}"
                )
            ],
            "worker_info": worker_result,
            "developer_needs_rework": False,
        }
    else:
        # ... existing error handling ...
```

---

### 3. Extend Engineering Subgraph State

#### Location
`services/langgraph/src/subgraphs/engineering.py`

#### Changes
Add rework tracking fields to `EngineeringState` (after line 73):

```python
class EngineeringState(TypedDict):
    """State for the engineering subgraph."""
    
    # ... existing fields ...
    
    # Developer rework tracking
    developer_needs_rework: bool  # True if commit/push validation failed
    developer_feedback: str | None  # Feedback message for rework
    developer_iteration_count: int  # Number of rework attempts
```

---

### 4. Add Developer Rework Node

#### Location
`services/langgraph/src/subgraphs/engineering.py`

#### Changes
Add new node class (after `BlockedNode`, around line 224):

```python
MAX_DEVELOPER_ITERATIONS = 2  # Max rework attempts before blocking


class DeveloperReworkNode(FunctionalNode):
    """Handle developer rework when commit/push validation fails."""
    
    def __init__(self):
        super().__init__(node_id="developer_rework")
    
    async def run(self, state: EngineeringState) -> dict:
        """Prepare state for developer rework iteration."""
        import structlog
        logger = structlog.get_logger()
        
        iteration_count = state.get("developer_iteration_count", 0) + 1
        feedback = state.get("developer_feedback", "")
        
        logger.info(
            "developer_rework_triggered",
            iteration_count=iteration_count,
            feedback=feedback,
        )
        
        # Clear rework flags, increment counter, add feedback to messages
        from langchain_core.messages import HumanMessage
        
        return {
            "messages": [
                HumanMessage(
                    content=f"🔄 Rework needed (attempt {iteration_count}/{MAX_DEVELOPER_ITERATIONS}):\n\n"
                    f"{feedback}"
                )
            ],
            "developer_needs_rework": False,
            "developer_feedback": None,
            "developer_iteration_count": iteration_count,
            "engineering_status": "reworking",
        }


developer_rework_node = DeveloperReworkNode()
```

---

### 5. Update Routing Logic

#### Location
`services/langgraph/src/subgraphs/engineering.py`

#### Changes

Replace `route_after_developer_spawn()` (line 141):

```python
def route_after_developer_spawn(state: EngineeringState) -> str:
    """Route after developer spawns worker.
    
    - If commit/push validation failed → rework (if iterations < MAX)
    - If max iterations reached → blocked
    - Otherwise → tester
    """
    import structlog
    logger = structlog.get_logger()
    
    if state.get("developer_needs_rework"):
        iteration_count = state.get("developer_iteration_count", 0)
        
        logger.info(
            "developer_validation_failed",
            iteration_count=iteration_count,
            max_iterations=MAX_DEVELOPER_ITERATIONS,
        )
        
        if iteration_count >= MAX_DEVELOPER_ITERATIONS:
            logger.warning("developer_max_iterations_reached")
            return "developer_blocked"
        
        return "developer_rework"
    
    logger.info("developer_validation_passed")
    return "tester"
```

---

### 6. Update Graph Topology

#### Location
`services/langgraph/src/subgraphs/engineering.py`

#### Changes

In `create_engineering_subgraph()` function:

**Add nodes** (after line 256):
```python
graph.add_node("developer_rework", developer_rework_node.run)
graph.add_node("developer_blocked", blocked_node.run)
```

**Update conditional edge** (replace line 297):
```python
graph.add_conditional_edges(
    "developer_spawn_worker",
    route_after_developer_spawn,
    {
        "tester": "tester",
        "developer_rework": "developer_rework",
        "developer_blocked": "developer_blocked",
    },
)
```

**Add rework loop edge** (after above):
```python
# Rework loops back to developer for retry
graph.add_edge("developer_rework", "developer")

# Blocked escalates to human
graph.add_edge("developer_blocked", END)
```

---

## Container Lifecycle Challenge

### Issue
Two conflicting requirements:
1. **Keep context:** Re-using the same container preserves Droid's context for rework
2. **Support parallelism:** Keeping containers alive blocks parallel usage by multiple users

### Current Approach: Respawn (Simple but Loses Context)
- Container uses `--rm` flag → auto-cleanup after completion
- Rework triggers new container spawn with feedback message
- **Pros:** Simple, supports parallelism
- **Cons:** Loses Droid context, less efficient

### Alternative: Container Persistence (Complex, Needs Design)
- Remove `--rm` flag conditionally
- Store container ID for reuse
- Implement container lifecycle management
- Add timeout-based cleanup
- **Pros:** Preserves context, more efficient
- **Cons:** Complex, requires orchestration, may block parallel usage

### Decision
**Phase 1 (This Plan):** Use respawn approach
- Simpler implementation
- Proven to work
- Supports multi-user scenarios

**Future Enhancement:** Container persistence requires separate brainstorming session to address:
- Multi-tenancy / resource isolation
- Container pool management
- Timeout and cleanup strategies
- Cost/benefit analysis

---

## Verification Plan

### 1. Unit Tests

**Test Developer Node Validation**

File: `services/langgraph/tests/unit/test_developer_validation.py` (NEW)

```python
"""Test Developer node commit/push validation."""

import pytest
from unittest.mock import AsyncMock, MagicMock
from langchain_core.messages import AIMessage

from services.langgraph.src.nodes.developer import developer_node
from services.langgraph.src.clients.worker_spawner import SpawnResult


@pytest.mark.asyncio
async def test_developer_spawn_success_with_commit():
    """Test successful developer spawn with commit SHA."""
    # Mock spawn result with commit
    mock_result = SpawnResult(
        request_id="test-123",
        success=True,
        exit_code=0,
        output="Success",
        commit_sha="abc123def456",
        branch="main",
    )
    
    state = {
        "repo_info": {"full_name": "test/repo", "name": "repo"},
        "project_spec": {"description": "Test project"},
        "developer_iteration_count": 0,
    }
    
    # Patch request_spawn
    with patch("services.langgraph.src.nodes.developer.request_spawn") as mock_spawn:
        mock_spawn.return_value = mock_result
        
        result = await developer_node.spawn_worker(state)
    
    assert result["developer_needs_rework"] is False
    assert "✅" in result["messages"][0].content
    assert "abc123def456" in result["messages"][0].content


@pytest.mark.asyncio
async def test_developer_spawn_success_without_commit():
    """Test developer spawn finishes but no commit SHA - should trigger rework."""
    mock_result = SpawnResult(
        request_id="test-123",
        success=True,
        exit_code=0,
        output="Success",
        commit_sha=None,  # No commit!
    )
    
    state = {
        "repo_info": {"full_name": "test/repo", "name": "repo"},
        "project_spec": {"description": "Test project"},
        "developer_iteration_count": 0,
    }
    
    with patch("services.langgraph.src.nodes.developer.request_spawn") as mock_spawn:
        mock_spawn.return_value = mock_result
        
        result = await developer_node.spawn_worker(state)
    
    assert result["developer_needs_rework"] is True
    assert "did NOT push changes" in result["messages"][0].content
    assert result["developer_feedback"] is not None
```

**Run command:**
```bash
make test-unit-langgraph
# Or specifically:
pytest services/langgraph/tests/unit/test_developer_validation.py -v
```

---

**Test Routing Logic**

File: `services/langgraph/tests/unit/test_developer_routing.py` (NEW)

```python
"""Test developer rework routing logic."""

from services.langgraph.src.subgraphs.engineering import (
    route_after_developer_spawn,
    MAX_DEVELOPER_ITERATIONS,
)


def test_route_with_rework_needed_first_attempt():
    """Test routing when rework is needed on first attempt."""
    state = {
        "developer_needs_rework": True,
        "developer_iteration_count": 0,
    }
    
    assert route_after_developer_spawn(state) == "developer_rework"


def test_route_with_rework_max_iterations():
    """Test routing when max iterations reached."""
    state = {
        "developer_needs_rework": True,
        "developer_iteration_count": MAX_DEVELOPER_ITERATIONS,
    }
    
    assert route_after_developer_spawn(state) == "developer_blocked"


def test_route_success_no_rework():
    """Test routing when validation passes."""
    state = {
        "developer_needs_rework": False,
        "developer_iteration_count": 0,
    }
    
    assert route_after_developer_spawn(state) == "tester"
```

**Run command:**
```bash
pytest services/langgraph/tests/unit/test_developer_routing.py -v
```

---

### 2. Integration Test

**Test End-to-End Rework Flow**

File: `services/langgraph/tests/integration/test_developer_rework_flow.py` (NEW)

```python
"""Integration test for developer rework flow."""

import pytest
from unittest.mock import patch, AsyncMock

from services.langgraph.src.subgraphs.engineering import create_engineering_subgraph


@pytest.mark.asyncio
async def test_developer_rework_flow():
    """Test that developer rework loop works end-to-end."""
    graph = create_engineering_subgraph()
    
    # Simulate developer spawn without commit on first try
    initial_state = {
        "messages": [],
        "repo_info": {"full_name": "test/repo"},
        "selected_modules": ["backend"],
        "project_spec": {"description": "Test"},
        "developer_iteration_count": 0,
    }
    
    # Mock first spawn: no commit
    mock_spawn_no_commit = AsyncMock(return_value={
        "success": True,
        "commit_sha": None,
    })
    
    # Mock second spawn: with commit
    mock_spawn_with_commit = AsyncMock(return_value={
        "success": True,
        "commit_sha": "abc123",
    })
    
    # TODO: Implement full graph execution test
    # This requires mocking multiple nodes and tracking state transitions
    # Pattern: mock all external calls, verify state updates through graph
```

**Run command:**
```bash
pytest services/langgraph/tests/integration/test_developer_rework_flow.py -v
```

---

### 3. Manual Verification

**Scenario: Developer Doesn't Push**

1. Start services: `make up`
2. Modify Factory.ai Droid to simulate no-push scenario (temporary hack):
   - In `services/coding-worker/entrypoint.sh`, comment out the push command
3. Create a test project via Telegram bot
4. Monitor logs: `docker logs -f codegen_orchestrator-langgraph-1`
5. Verify:
   - ✅ Developer node detects missing commit SHA
   - ✅ State includes `developer_needs_rework: true`
   - ✅ Rework node is triggered
   - ✅ Developer spawns again with feedback
6. Restore entrypoint.sh
7. Verify second attempt succeeds with commit

---

## Implementation Checklist

- [ ] Update `scripts/cli_agent_configs.yaml` with developer prompt
- [ ] Run seeding script to update database
- [ ] Modify `services/langgraph/src/nodes/developer.py` - add validation
- [ ] Extend `EngineeringState` in `services/langgraph/src/subgraphs/engineering.py`
- [ ] Add `DeveloperReworkNode` class
- [ ] Update `route_after_developer_spawn()` function
- [ ] Update graph topology with rework edges
- [ ] Write unit tests for validation logic
- [ ] Write unit tests for routing logic
- [ ] Write integration test for rework flow
- [ ] Run all tests and verify passing
- [ ] Manual verification with modified worker
- [ ] Document container lifecycle challenge as future work

---

## Future Work

### Container Persistence (Requires Design Session)

**Goals:**
- Preserve Droid context across rework iterations
- Support concurrent multi-user usage
- Efficient resource management

**Challenges:**
- Resource isolation (multiple users = multiple containers)
- Container lifecycle (when to cleanup?)
- Failure scenarios (orphaned containers?)
- Cost/complexity vs. benefit

**Proposed Approach:**
1. Brainstorming session to design solution
2. Proof of concept with single-user scenario
3. Extend to multi-user with container pool
4. Production hardening

**Not Blocking:** Current respawn approach is functional and will be replaced when container persistence is ready.

---

## Summary

This plan adds commit/push validation to the Developer node with a simple rework loop. It:
- ✅ Updates prompt template via seeding (no migrations)
- ✅ Validates commit SHA after developer execution
- ✅ Provides rework mechanism with iteration limits
- ✅ Uses simple respawn approach (defers container persistence)
- ✅ Includes comprehensive testing strategy
- ✅ Documents future enhancement path

The implementation is incremental, testable, and doesn't block on complex container lifecycle management.
