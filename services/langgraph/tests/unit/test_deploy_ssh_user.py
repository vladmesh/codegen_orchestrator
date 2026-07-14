from pathlib import Path

RUNTIME_SSH_PATHS = (
    "src/consumers/_qa_runner.py",
    "src/consumers/deploy_lifecycle.py",
    "src/consumers/deploy_precheck.py",
    "src/subgraphs/devops/deployer.py",
    "src/subgraphs/devops/smoke.py",
)


def test_runtime_deploy_paths_do_not_hardcode_root_user():
    service_dir = Path(__file__).parents[2]

    for relative_path in RUNTIME_SSH_PATHS:
        source = (service_dir / relative_path).read_text()
        assert 'username="root"' not in source
        assert '"DEPLOY_USER": "root"' not in source
