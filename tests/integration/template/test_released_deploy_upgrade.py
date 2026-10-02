"""Real released Copier updates and installed native transport, without connections."""

import ast
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest
import yaml

from scripts.template_pin import TEMPLATE_PIN

ROOT = Path(__file__).resolve().parents[3]
WORKFLOW = Path(".github/workflows/deploy.yml")
PREVIOUS_REF = ".".join(map(str, (0, 6, 3)))
RELEASE_SHA = "1de7aa6c02cfcf212b2d21919defbb3d77383998"


def run(command, cwd, **kwargs):
    result = subprocess.run(command, cwd=cwd, capture_output=True, text=True, timeout=180, **kwargs)
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout


def git(product, *args):
    return run(["git", *args], product)


def workflow(product):
    return yaml.load((product / WORKFLOW).read_text(), Loader=yaml.BaseLoader)  # noqa: S506 (strings only)


def executable_digest(product):
    from hashlib import sha256

    steps = [
        {k: v for k, v in step.items() if k != "name"}
        for step in workflow(product)["jobs"]["deploy"]["steps"]
    ]
    return sha256(json.dumps(steps, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def admission_digest():
    tree = ast.parse(
        (ROOT / "services/langgraph/src/subgraphs/devops/deploy_workflow.py").read_text()
    )
    return next(
        ast.literal_eval(node.value)
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "RELEASED_STEPS_DIGEST" for t in node.targets)
    )


def capture_copy(product, tmp_path, host):
    """Run the released shell, capturing each command before external effects."""
    directory = tmp_path / "capture-bin"
    directory.mkdir(exist_ok=True)
    log = tmp_path / "capture.jsonl"
    capture = (
        "#!/usr/bin/env python3\nimport json,os,sys\nfrom pathlib import Path\n"
        'with Path(os.environ["CAPTURE_LOG"]).open("a") as f:\n'
        '    f.write(json.dumps([Path(sys.argv[0]).name,*sys.argv[1:]])+"\\n")\n'
    )
    for name in ("ssh", "scp", "sleep"):
        path = directory / name
        path.write_text(capture)
        path.chmod(0o755)
    step = workflow(product)["jobs"]["deploy"]["steps"][2]["run"]
    for key, value in {
        "DEPLOY_HOST": host,
        "DEPLOY_USER": "transport-user",
        "PROJECT_NAME": "upgrade-proof",
    }.items():
        step = step.replace("${{ secrets." + key + " }}", value)
    run(
        ["bash", "-c", step],
        product,
        env=os.environ
        | {"PATH": str(directory) + ":" + os.environ["PATH"], "CAPTURE_LOG": str(log)},
    )
    records = [json.loads(line) for line in log.read_text().splitlines()]
    log.unlink()
    ssh = next(record[1:] for record in records if record[0] == "ssh")
    scp = next(record[1:] for record in records if record[0] == "scp")
    assert ssh[-2:] == [f"transport-user@{host}", "mkdir -p /opt/services/upgrade-proof/infra"]
    assert scp[-3:-1] == ["infra/compose.base.yml", "infra/compose.prod.yml"]
    scp_host = "[" + host + "]" if ":" in host else host
    assert scp[-1] == f"transport-user@{scp_host}:/opt/services/upgrade-proof/infra/"
    assert not any(record[0] == "sleep" for record in records)
    return ssh, scp


def native_host(product, tmp_path, destination, legacy=False):
    """Installed SCP parses its own destination; fake SSH exits before connecting."""
    capture = tmp_path / "native-ssh"
    record = tmp_path / "native-args.json"
    capture.write_text(
        "#!/usr/bin/env python3\nimport json,os,sys\nfrom pathlib import Path\n"
        'Path(os.environ["NATIVE_RECORD"]).write_text(json.dumps(sys.argv[1:]))\n'
        "sys.exit(1)\n"
    )
    capture.chmod(0o755)
    result = subprocess.run(
        [
            "scp",
            *(["-O"] if legacy else []),
            "-S",
            str(capture),
            "infra/compose.base.yml",
            destination,
        ],
        cwd=product,
        env=os.environ | {"NATIVE_RECORD": str(record)},
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode != 0
    args = json.loads(record.read_text())
    index = args.index("-l")
    assert args[index + 1] == "transport-user"
    return args[args.index("--") + 1]


def reconcile_owned_workflow(product, previous):
    """Review the two known local changes; retain the name and released runner."""
    rejected = product / (str(WORKFLOW) + ".rej")
    rejection = rejected.read_text()
    assert "+    runs-on: self-hosted" in rejection
    additions = {
        line[1:]
        for line in rejection.splitlines()
        if line.startswith("+") and not line.startswith("+++")
    }
    assert additions <= {"name: Owned deploy", "    runs-on: self-hosted"}
    before = yaml.load(git(product, "show", f"{previous}:{WORKFLOW}"), Loader=yaml.BaseLoader)  # noqa: S506
    assert before["name"] == "Owned deploy"
    assert before["jobs"]["deploy"]["runs-on"] == "self-hosted"
    path = product / WORKFLOW
    # Git versions can reject the name in the same hunk as the runner. Restore
    # that committed customization explicitly before the final full readback.
    path.write_text(path.read_text().replace("name: Deploy", "name: Owned deploy", 1))
    rejected.unlink()


@pytest.fixture
def released_product(tmp_path):
    product = tmp_path / "product"
    run(
        [
            "copier",
            "copy",
            "--defaults",
            "--trust",
            f"--vcs-ref={PREVIOUS_REF}",
            "--data=project_name=upgrade_proof",
            "--data=modules=backend,tg_bot",
            TEMPLATE_PIN.source,
            str(product),
        ],
        ROOT,
    )
    assert yaml.safe_load((product / ".copier-answers.yml").read_text())["_commit"] == PREVIOUS_REF
    return product


@pytest.mark.parametrize("overlap", [False, True])
def test_published_update_retains_owned_bytes_and_requires_reconciliation(
    released_product, tmp_path, overlap
):
    product = released_product
    owned = [
        ".env",
        ".env.example",
        "shared/spec/models.yaml",
        "shared/spec/events.yaml",
        "services/backend/src/app/owned.py",
        "services/backend/src/controllers/owned.py",
        "services/backend/src/app/models/user.py",
        "services/tg_bot/src/app/owned.py",
    ]
    for name in owned:
        path = product / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(
            (path.read_bytes() if path.exists() else b"") + b"\n# owned synthetic upgrade data\n"
        )
    (product / ".env").write_text("SYNTHETIC_ENV=owned-upgrade-proof\n")
    before = {name: (product / name).read_bytes() for name in owned}
    source = (product / WORKFLOW).read_text().replace("name: Deploy", "name: Owned deploy", 1)
    if overlap:
        source = source.replace("runs-on: ubuntu-latest", "runs-on: self-hosted")
    (product / WORKFLOW).write_text(source)
    git(product, "init", "--quiet")
    git(product, "add", "-A")
    git(product, "add", "--force", ".env")
    git(
        product,
        "-c",
        "user.name=Upgrade tests",
        "-c",
        "user.email=tests@example.com",
        "commit",
        "--quiet",
        "-m",
        "Owned synthetic baseline",
    )
    assert git(product, "status", "--porcelain") == ""
    previous = git(product, "rev-parse", "HEAD").strip()
    git(product, "switch", "-c", "review-kit-update")
    assert (
        native_host(product, tmp_path, "transport-user@2001:db8::42:/tmp/upgrade-proof/") == "2001"
    )
    run(
        [
            "copier",
            "update",
            "--defaults",
            "--trust",
            f"--vcs-ref={TEMPLATE_PIN.ref}",
            "--conflict=rej",
        ],
        product,
    )
    assert before == {name: (product / name).read_bytes() for name in owned}
    answers = yaml.safe_load((product / ".copier-answers.yml").read_text())
    assert answers["_src_path"] == TEMPLATE_PIN.source
    assert answers["_commit"] == TEMPLATE_PIN.ref
    assert answers["modules"] == "backend,tg_bot"
    assert f"codegen-product-kit.git@{RELEASE_SHA}" in (product / "pyproject.toml").read_text()
    assert f"rev={RELEASE_SHA}#{RELEASE_SHA}" in (product / "uv.lock").read_text()
    if overlap:
        reconcile_owned_workflow(product, previous)
    updated = workflow(product)
    assert updated["name"] == "Owned deploy"
    assert updated["jobs"]["deploy"]["runs-on"] == "ubuntu-24.04"
    assert updated["jobs"]["deploy"]["steps"][3]["with"]["host"] == "${{ secrets.DEPLOY_HOST }}"
    assert executable_digest(product) == admission_digest()
    assert not list(product.rglob("*.rej"))
    assert before == {name: (product / name).read_bytes() for name in owned}
    for host in ("192.0.2.42", "2001:db8::42", "::ffff:192.0.2.42"):
        ssh, scp = capture_copy(product, tmp_path, host)
        config = run(["ssh", "-G", *ssh[:-1]], product)
        assert f"hostname {host}\n" in config
        for legacy in (False, True):
            assert native_host(product, tmp_path, scp[-1], legacy=legacy) == host
    transport = updated["jobs"]["deploy"]["steps"][2]["run"]
    assert "for attempt in 1 2 3; do" in transport and "attempt * 15" in transport
    git(product, "add", "-A")
    git(
        product,
        "-c",
        "user.name=Upgrade tests",
        "-c",
        "user.email=tests@example.com",
        "commit",
        "--quiet",
        "-m",
        "Reviewed released transport update",
    )
    built = git(product, "rev-parse", "HEAD").strip()
    assert built != previous and git(product, "status", "--porcelain") == ""
    assert git(product, "show", f"{built}:{WORKFLOW}") == (product / WORKFLOW).read_text()
    shutil.rmtree(product)
