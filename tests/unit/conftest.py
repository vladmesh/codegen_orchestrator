"""Fixtures shared by the repository-level unit tests."""

# The architectural guards read the tree through one session-wide parse.
from shared.tests.source_index import production_source_index  # noqa: F401
