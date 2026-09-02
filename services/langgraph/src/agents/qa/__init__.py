"""The central QA agent: one target, typed tools, no shell."""

from .tools import build_qa_callables

__all__ = ["build_qa_callables"]
