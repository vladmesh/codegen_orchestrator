# Test Compose Harnesses

Dockerfiles and Compose definitions used only by automated tests live here so the repository root
does not present test infrastructure as a production Docker subsystem.

- `service/` runs one service with its direct dependencies through `make test-service`.
- `integration/` runs cross-service suites through `make test-integration-*` and CI.
- `e2e/` is the legacy, currently unclaimed harness for `tests/e2e`; it remains visible pending the
  recorded decision to either migrate its unique coverage or remove it.

Production topology remains in the root `docker-compose*.yml` files and operational configuration
remains under `infra/`.
