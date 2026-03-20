"""Background tasks for scheduler service.

Imports are lazy to avoid triggering Settings validation at import time
(needed for unit tests that don't set env vars).
"""

__all__ = [
    "analytics_aggregator_worker",
    "sync_projects_worker",
    "sync_servers_worker",
    "health_check_worker",
    "publish_provisioner_trigger",
    "retry_pending_servers",
    "rag_summarizer_worker",
    "process_provisioner_result",
]


def __getattr__(name: str):
    _imports = {
        "analytics_aggregator_worker": ".analytics_aggregator",
        "sync_projects_worker": ".github_sync",
        "health_check_worker": ".health_checker",
        "process_provisioner_result": ".provisioner_result_listener",
        "publish_provisioner_trigger": ".provisioner_trigger",
        "retry_pending_servers": ".provisioner_trigger",
        "rag_summarizer_worker": ".rag_summarizer",
        "sync_servers_worker": ".server_sync",
    }
    if name in _imports:
        import importlib

        module = importlib.import_module(_imports[name], __package__)
        return getattr(module, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
