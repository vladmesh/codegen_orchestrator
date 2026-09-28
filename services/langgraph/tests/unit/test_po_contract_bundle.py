"""Worker DTO imports do not require service transport or encryption modules."""

import os
from pathlib import Path
import re
import shutil
import subprocess
import sys


def test_worker_shared_bundle_imports_without_service_modules(tmp_path):
    root = Path(__file__).resolve().parents[4]
    dockerfile = root / "services/worker-manager/images/worker-base-common/Dockerfile"
    for source, destination in re.findall(
        r"^COPY (shared/\S+) /app/(shared/\S+)$", dockerfile.read_text(), re.M
    ):
        target = tmp_path / destination
        target.parent.mkdir(parents=True, exist_ok=True)
        if (root / source).is_dir():
            shutil.copytree(root / source, target)
        else:
            shutil.copyfile(root / source, target)
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; from pathlib import Path; "
            "sys.path = [p for p in sys.path if p not in "
            "(sys.argv[2], str(Path(sys.argv[2]) / 'src'))]; "
            "sys.path.insert(0, sys.argv[1]); "
            "from shared.contracts.queues import worker_result; "
            "assert Path(worker_result.__file__).is_relative_to(sys.argv[1]); "
            "assert 'shared.crypto' not in sys.modules; "
            "assert 'shared.queues' not in sys.modules",
            str(tmp_path),
            str(root),
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr
