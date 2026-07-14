# Template Integration Tests
This directory contains the service-template integration contract tests.

Run the complete contract, including the Stage 5 worker-mode smoke, with:

```bash
make test-integration-template
```

The Stage 5 test scaffolds `gh:vladmesh/service-template` at the pinned `0.3.0`
release into a unique temporary workspace. It uses a unique Compose project name,
runs the generated project's setup, quality and worker-mode targets, then removes
only resources carrying that Compose project label.
