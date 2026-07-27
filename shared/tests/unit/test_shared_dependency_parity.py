"""Every consumer of `shared` must declare `shared`'s third-party dependencies.

`shared` is not pip-installed, so its `dependencies` list installs nothing. It
reaches consumers as a bind-mount (compose) or a `COPY shared` (images), and each
of them has to repeat those dependencies in its own pyproject.toml. That
duplication was maintained by hand across seven files, and a miss surfaced only
as an ImportError at runtime.

The consumer set is derived from the delivery channels themselves — compose bind
mounts and `COPY shared` lines in Dockerfiles — so adding a new consumer cannot
quietly skip the check.
"""

from pathlib import Path
import re
import tomllib

import pytest
import yaml

REPO_ROOT = Path(__file__).parents[3]
SHARED_MOUNT = "./shared:/app/shared"
COPY_SHARED = re.compile(r"^\s*COPY\s+(?:--\S+\s+)*shared/?\s", re.MULTILINE)


def _canonical(requirement: str) -> str:
    """`PyYAML>=6.0.2` -> `pyyaml`, `pyjwt[crypto]>=2.8.0` -> `pyjwt`."""
    name = re.split(r"[\[<>=!~;\s]", requirement.strip(), maxsplit=1)[0]
    return name.lower().replace("_", ".").replace("-", ".")


def _declared_dependencies(pyproject: Path) -> set[str]:
    data = tomllib.loads(pyproject.read_text())
    return {_canonical(dep) for dep in data["project"]["dependencies"]}


def _pyprojects_referenced_by(dockerfile: Path) -> set[Path]:
    """The pyproject files a Dockerfile installs from.

    Services install either from their pyproject directly or from the
    `requirements.lock` compiled out of it; the worker base image reads
    `packages/worker-wrapper/pyproject.toml`.
    """
    found = set()
    for token in re.findall(
        r"[\w./-]+(?:pyproject\.toml|requirements\.lock)", dockerfile.read_text()
    ):
        candidate = REPO_ROOT / token
        if candidate.name == "requirements.lock":
            candidate = candidate.with_name("pyproject.toml")
        if candidate.is_file():
            found.add(candidate)
    return found


def _compose_consumers() -> set[Path]:
    compose = yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text())
    consumers = set()
    for service in compose["services"].values():
        if not any(SHARED_MOUNT in str(volume) for volume in service.get("volumes", [])):
            continue
        pyproject = REPO_ROOT / Path(service["build"]["dockerfile"]).parent / "pyproject.toml"
        assert pyproject.is_file(), f"{pyproject} missing for a bind-mounted service"
        consumers.add(pyproject)
    return consumers


def _copy_consumers() -> set[Path]:
    consumers = set()
    for dockerfile in REPO_ROOT.glob("**/Dockerfile*"):
        if ".venv" in dockerfile.parts or not COPY_SHARED.search(dockerfile.read_text()):
            continue
        consumers |= _pyprojects_referenced_by(dockerfile)
    return consumers


CONSUMERS = sorted(_compose_consumers() | _copy_consumers())
SHARED_DEPENDENCIES = _declared_dependencies(REPO_ROOT / "shared" / "pyproject.toml")


def test_consumer_discovery_covers_every_bind_mounted_service():
    """Guard the discovery itself: a compose consumer must never drop out."""
    assert _compose_consumers() <= set(CONSUMERS)
    assert {p.parent.name for p in CONSUMERS} >= {
        "api",
        "langgraph",
        "telegram_bot",
        "scheduler",
        "worker-manager",
        "infra-service",
        "scaffolder",
        "worker-wrapper",
    }


@pytest.mark.parametrize("consumer", CONSUMERS, ids=lambda p: p.parent.name)
def test_consumer_declares_every_shared_dependency(consumer: Path):
    missing = SHARED_DEPENDENCIES - _declared_dependencies(consumer)

    assert not missing, (
        f"{consumer.relative_to(REPO_ROOT)} ships shared but does not declare "
        f"{sorted(missing)}; shared/pyproject.toml requires them"
    )
