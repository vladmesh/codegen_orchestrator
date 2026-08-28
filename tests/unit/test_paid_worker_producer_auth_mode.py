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
            for keyword in config.keywords:
                if keyword.arg != "env_vars" or not isinstance(keyword.value, ast.Dict):
                    continue
                names = {
                    key.value
                    for key in keyword.value.keys
                    if isinstance(key, ast.Constant) and isinstance(key.value, str)
                }
                assert not {"CLAUDE_CODE_OAUTH_TOKEN", "CODEX_ACCESS_TOKEN"}.intersection(names), (
                    f"{producer} must not carry a stand credential through the worker payload"
                )


def test_stand_token_mode_serializes_only_its_non_secret_selector():
    config = WorkerConfig(
        name="stand-worker",
        worker_type="developer",
        agent_type="claude",
        instructions="test",
        allowed_commands=[],
        capabilities=[],
        auth_mode="stand_token",
        ownership={"project_id": "project", "run_id": "run", "attempt_id": "attempt"},
    )

    payload = config.model_dump_json()

    assert '"auth_mode":"stand_token"' in payload
    for forbidden in ("CLAUDE_CODE_OAUTH_TOKEN", "CODEX_ACCESS_TOKEN", "fake-token"):
        assert forbidden not in payload
