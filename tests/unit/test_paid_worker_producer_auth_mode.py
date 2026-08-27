"""The executor diagnostic covers the only modes paid producers can select."""

import ast
from pathlib import Path

from shared.contracts.queues.worker import WorkerConfig

ROOT = Path(__file__).resolve().parents[2]
PAID_PRODUCERS = (
    ROOT / "services/langgraph/src/clients/worker_spawner.py",
    ROOT / "services/langgraph/src/clients/qa_worker.py",
)


def test_paid_producers_are_pinned_to_the_host_session_diagnostic_mode():
    assert WorkerConfig.model_fields["auth_mode"].default == "host_session"
    for producer in PAID_PRODUCERS:
        tree = ast.parse(producer.read_text(), filename=str(producer))
        worker_configs = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "WorkerConfig"
        ]
        assert worker_configs, f"{producer} must construct WorkerConfig"
        for config in worker_configs:
            keywords = {keyword.arg for keyword in config.keywords}
            assert "auth_mode" not in keywords, (
                f"{producer} selects an auth mode without a corresponding executor diagnostic"
            )
            assert "api_key" not in keywords, (
                f"{producer} selects API-key authentication without an executor diagnostic"
            )
