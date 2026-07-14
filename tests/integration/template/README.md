# Template Integration Tests
This directory contains the service-template integration contract tests.

Run the production baseline compatibility smoke with:

```bash
make test-template-compat ARTIFACT_DIR=/tmp/template-compat-baseline
```

The command reads `scheduler.service_template_source` and
`scheduler.service_template_ref` directly from `scripts/system_configs.yaml`.
To check a future release tag or commit without editing that production pin:

```bash
make test-template-compat TEMPLATE_REF=<tag-or-commit> ARTIFACT_DIR=/tmp/template-compat-candidate
```

Each entry scaffolds into an independent workspace with a unique Compose project,
runs setup, lint, tests, worker start/probe/call and verified cleanup. Its JSON
artifact records the requested source/ref and resolved commit SHA. A green candidate
is the gate for a separate PR that updates the production pin; the smoke never edits
the pin itself.
