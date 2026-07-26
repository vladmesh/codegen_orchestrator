"""Regression checks for the removed external trace receiver."""

from pathlib import Path

LANGGRAPH_DIR = Path(__file__).resolve().parents[2]


def test_langgraph_does_not_configure_an_external_trace_exporter():
    consumer_sources = "\n".join(
        (LANGGRAPH_DIR / "src" / "consumers" / name).read_text()
        for name in ("po.py", "architect.py", "engineering.py", "deploy.py")
    )

    assert "get_langfuse_callbacks" not in consumer_sources
    assert "build_langfuse_metadata" not in consumer_sources
    assert not (LANGGRAPH_DIR / "src" / "tracing.py").exists()
