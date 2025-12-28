"""TASK.md template for Developer agent.

This file defines the task instructions that will be placed in the project
repository for the Developer (Factory.ai/Claude Code) to follow.
"""

TASK_MD_TEMPLATE = """# Task: Implement {project_name}

## Project Description

{description}

## Detailed Specification

{detailed_spec}

## Selected Modules

The project has been initialized with the following modules: **{modules}**

## Your Task

You are the Developer agent. The project structure has been prepared using service-template.

### Step 1: Understand the Structure

- Read `AGENTS.md` for framework conventions
- Explore `services/` directory to see available modules
- Check `shared/spec/` for spec file locations and examples

### Step 2: Define Specifications

Based on the project requirements above, create YAML specifications:

1. **Models** (`shared/spec/models.yaml`):
   - Define all data models with their fields
   - Include variants (Create, Update, Read) where needed
   - See existing examples in the file

2. **Domain Specs** (`services/<module>/spec/<domain>.yaml`):
   - Define operations (CRUD, custom actions)
   - Map operations to REST endpoints
   - Configure events if async processing is needed

### Step 3: Generate Code

Run:
```bash
make generate-from-spec
```

This will generate:
- Pydantic schemas in `shared/shared/generated/`
- FastAPI routers in `services/*/src/generated/routers/`
- Protocol interfaces in `services/*/src/generated/protocols.py`
- HTTP clients for service-to-service communication

### Step 4: Implement Controllers

- Implement business logic in `services/*/src/controllers/`
- Follow the Protocol interface from generated code
- Use async patterns (Python 3.12+)
- Use structlog for logging

### Step 5: Test

Run:
```bash
make test
```

Ensure all tests pass before committing.

### Step 6: Commit and Push

```bash
git add .
git commit -m "Implement {project_name} business logic"
git push
```

## Custom Instructions

{custom_instructions}

## Important Notes

- Do NOT modify files in `src/generated/` directories - they are auto-generated
- Follow the patterns described in `AGENTS.md`
- All code must be async-ready (Python 3.12+)
- Use structlog for logging: `logger.info("event_name", key=value)`
- Keep controllers focused on business logic; infrastructure is handled by generated code
"""


def render_task_md(
    project_name: str,
    description: str,
    detailed_spec: str,
    modules: list[str],
    custom_instructions: str | None = None,
) -> str:
    """Render TASK.md template with project context.

    Args:
        project_name: Name of the project (snake_case)
        description: Short project description
        detailed_spec: Full markdown specification from Analyst
        modules: List of selected modules (e.g., ["backend", "tg_bot"])
        custom_instructions: Optional custom instructions from Architect

    Returns:
        Rendered TASK.md content
    """
    return TASK_MD_TEMPLATE.format(
        project_name=project_name,
        description=description or "No description provided",
        detailed_spec=detailed_spec or "See project description above.",
        modules=", ".join(modules) if modules else "backend",
        custom_instructions=custom_instructions or "No additional instructions.",
    )
