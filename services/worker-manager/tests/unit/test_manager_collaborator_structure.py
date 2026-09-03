"""Regression coverage for WorkerManager collaborator boundaries."""

from src.manager import WorkerManager


def test_manager_keeps_only_public_diagnostic_and_removal_facades():
    from src.executor_diagnostics import ExecutorDiagnostics
    from src.worker_removal import WorkerRemoval

    assert ExecutorDiagnostics is not None
    assert WorkerRemoval is not None
    assert hasattr(WorkerManager, "publish_executor_diagnostics")
    assert hasattr(WorkerManager, "delete_worker")
    for private_implementation in (
        "_executor_leases",
        "_executor_diagnostic",
        "stand_token_failures",
        "_ownership_from_meta",
        "_capture_removal_evidence",
        "_unreadable_removal_evidence",
        "_read_removal_evidence",
        "_bounded_tail",
        "_store_removal_evidence",
        "_worker_type_fact",
    ):
        assert not hasattr(WorkerManager, private_implementation)
