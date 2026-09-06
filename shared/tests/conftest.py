"""Pytest collection boundaries for shared tests."""

# This is a complete generated project fixture. Its own tests execute only in
# the generated project's compatibility smoke, never as orchestrator tests. The
# directory is named after whichever template the pin names, and everything under
# `fixtures/` is that render, so the glob names the tree and not the template.
# `shared` is baked into images that carry no `scripts/`, so this imports no pin.
collect_ignore_glob = ["fixtures/*/**"]
