"""Tests for the one-shot provisioner graph boundary."""


def test_provisioner_graph_has_no_process_local_checkpointer():
    from src.graph import create_graph

    graph = create_graph()

    assert graph.checkpointer is None
