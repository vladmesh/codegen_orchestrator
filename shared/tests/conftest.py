"""Pytest collection boundaries for shared tests."""

# This is a complete generated project fixture. Its own tests execute only in
# the generated project's compatibility smoke, never as orchestrator tests.
collect_ignore_glob = ["fixtures/service-template-*/**"]
