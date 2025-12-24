# Project Agent Instructions

This project was generated using [service-template](https://github.com/vladmesh/service-template).

## 🛠 Technology Stack

| Component | Technology |
|-----------|------------|
| Language | Python 3.12 |
| Framework | FastAPI |
| Database | PostgreSQL + SQLAlchemy (async) |
| Messaging | Redis Pub/Sub |
| Code Generation | YAML Specs → Python |

## 📂 Project Structure

```
project/
├── domains/           # Domain specifications (YAML)
│   └── *.yaml        # Define models, operations, events
├── src/
│   ├── app/          # Generated application code
│   │   ├── domains/  # Generated domain code
│   │   ├── models/   # SQLAlchemy models
│   │   └── api/      # REST API routes
│   └── controllers/  # Business logic (YOU IMPLEMENT THIS)
├── tests/            # Test files
├── Makefile          # Common commands
└── docker-compose.yml
```

## 🔧 Core Commands

```bash
# Generate code from specs
make generate

# Run linters (ruff + mypy)
make lint

# Run tests
make test

# Start development server
make dev
```

## 📝 Workflow: Adding New Features

### 1. Define Domain Specification

Create/update `domains/<domain>.yaml`:

```yaml
name: weather
version: "1.0"

models:
  WeatherData:
    fields:
      - name: city
        type: str
      - name: temperature
        type: float
      - name: humidity
        type: int

operations:
  get_weather:
    type: query
    input_model: CityRequest
    output_model: WeatherData
    transport:
      rest:
        method: GET
        path: /weather/{city}
```

### 2. Generate Code

```bash
make generate
```

This creates:
- `src/app/domains/<domain>/models.py` - Pydantic models
- `src/app/domains/<domain>/protocols.py` - Controller interface
- `src/app/domains/<domain>/router.py` - FastAPI routes

### 3. Implement Controller

Create `src/controllers/<domain>_controller.py`:

```python
from src.app.domains.<domain>.protocols import <Domain>ControllerProtocol

class <Domain>Controller(<Domain>ControllerProtocol):
    async def get_weather(self, city: str) -> WeatherData:
        # Your business logic here
        ...
```

### 4. Run Tests

```bash
make test
```

## ⚠️ Critical Rules

1. **Never edit generated files** in `src/app/domains/` - they will be overwritten
2. **Always run `make lint`** before committing
3. **Use async/await** everywhere - no blocking operations
4. **No default values for secrets** - use environment variables

## 🏗 Architecture Principles

- **Spec-first**: Define schemas in YAML, generate code
- **Clean separation**: Generated code + manual controllers
- **Type safety**: Full mypy strict mode
- **Async by default**: All I/O operations are async
