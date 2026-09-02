# Test Compose Harnesses

Dockerfiles and Compose definitions used only by automated tests live here so the repository root
does not present test infrastructure as a production Docker subsystem.

- `service/` runs one service with its direct dependencies through `make test-service`.
- `integration/` runs cross-service suites through `make test-integration-*` and CI.

Production topology remains in the root `docker-compose*.yml` files and operational configuration
remains under `infra/`.
