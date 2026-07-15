from types import SimpleNamespace

from pipeline_helpers import find_worker_container


def test_worker_container_discovery_uses_manager_label(monkeypatch):
    calls = []

    def run(cmd, **kwargs):
        calls.append(cmd)
        return SimpleNamespace(returncode=0, stdout="custom-prefix-abc\n", stderr="")

    monkeypatch.setattr("pipeline_helpers.subprocess.run", run)

    assert find_worker_container("abc") == "custom-prefix-abc"
    assert "label=com.codegen.worker.id=abc" in calls[0]
    assert "worker-abc" not in calls[0]
