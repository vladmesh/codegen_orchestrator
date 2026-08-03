# `shared` is not a package

Decided 2026-08-03. Status: accepted.

## What was open

`shared/` had a declared package boundary that did not exist. The root `pyproject.toml` listed three
editable sources — `codegen-orchestrator-shared-contracts`, `-redis`, `-log-config` — backed by
`shared/contracts/pyproject.toml`, `shared/redis/pyproject.toml` and
`shared/log_config/pyproject.toml`. Nothing referenced those names: no service dependency, no
dependency group, and `uv.lock` did not contain them at all. They installed nothing and were read by
nobody, while looking like the real delivery mechanism to anyone opening the file.

Four routes were on the table:

- **A** — make `shared` a workspace member and install it properly.
- **B** — finish the "not a package" line: delete the dead declarations.
- **C** — split `shared` into real packages (contracts / redis / log_config) with real dependents.
- **D** — tie the circuits together with a check that catches a built stand lagging behind the tree.

## Decision

**B, paired with D.** B lands here; D follows as its own card so that a deletion and a new check do
not ride in together.

`shared` is already not a package in every way that matters, so B writes down what is true instead of
building something new. The three declarations were the only thing suggesting otherwise, and they were
actively misleading.

**A is rejected.** Making `shared` a workspace member means installing it into the venv, which touches
every Dockerfile and every compose service, and it takes away editing `shared/` on a live stand
without a rebuild. That property is worth more right now than package hygiene.

**C is rejected.** Cutting `shared` into real distributable packages is the most expensive rebuild of
the three and does not fit the sprint budget. It stays available later; nothing here forecloses it.

## What this leaves

`shared` has exactly one declared form: a tree with three delivery channels (bind-mount, `COPY shared`,
import from the tree via `PYTHONPATH`). `shared/pyproject.toml` stays as the single declaration of
`shared`'s third-party dependencies, installing nothing;
`shared/tests/unit/test_shared_dependency_parity.py` makes every consumer repeat them.

`tests/unit/test_uv_sources_are_used.py` fails if a `[tool.uv.sources]` entry that nothing depends on
comes back.
