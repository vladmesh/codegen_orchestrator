#!/usr/bin/env python3
"""Build each backend service image, import its runtime modules and check its lock inside it.

One build per image serves both checks: the entrypoint imports, and the lock check of
scripts/service_image_locks.py (the image's installed distributions equal its
requirements.lock, and the lock still satisfies its pyproject.toml).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import shlex
import subprocess
import sys
import time

import yaml

try:
    from scripts import service_image_locks
except ModuleNotFoundError:  # Script execution puts scripts/, not the root, on sys.path.
    import service_image_locks

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ROOT / "docker-compose.yml"
SERVICE_IMAGE_LIST = ROOT / "infra" / "scripts" / "service-images.sh"


@dataclass(frozen=True)
class ServiceImage:
    name: str
    dockerfile: str
    default_module: str

    @property
    def tag(self) -> str:
        return f"codegen-orchestrator/{self.name}:entrypoint-import"

    @property
    def lock(self) -> Path:
        return ROOT / Path(self.dockerfile).parent / service_image_locks.LOCK

    @property
    def pyproject(self) -> Path:
        return ROOT / Path(self.dockerfile).parent / service_image_locks.PYPROJECT


# Keep this to production services. Test-runner images do not ship an application
# entrypoint, while these are the images CI and deployment start as services.
SERVICE_IMAGES = (
    ServiceImage("api", "services/api/Dockerfile", "src.main"),
    ServiceImage("infra-service", "services/infra-service/Dockerfile", "src.main"),
    ServiceImage("langgraph", "services/langgraph/Dockerfile", "src.main"),
    ServiceImage("scaffolder", "services/scaffolder/Dockerfile", "src.main"),
    ServiceImage("scheduler", "services/scheduler/Dockerfile", "src.pipeline"),
    ServiceImage("telegram_bot", "services/telegram_bot/Dockerfile", "src.main"),
    ServiceImage("worker-broker", "services/worker-broker/Dockerfile", "src.main"),
    ServiceImage("worker-manager", "services/worker-manager/Dockerfile", "src.main"),
)

# `src.main` imports this consumer only after a PO message arrives. It remains a
# shipped LangGraph entry module and so must be checked with the eager consumers.
EXTRA_IMPORT_MODULES = {"langgraph": ("src.consumers.po",)}

# These are deliberately inert values. The check imports modules only and must not
# connect to a service, but settings modules validate their required values at import.
IMPORT_ENV = {
    "API_BASE_URL": "http://127.0.0.1:9",
    "BROKER_INTERNAL_TOKEN": "test-worker-broker-internal-token",
    "DATABASE_URL": "postgresql+asyncpg://test:test@127.0.0.1:5432/test",
    "DEFAULT_AGENT_TYPE": "claude",
    "GITHUB_APP_ID": "12345",
    "GITHUB_APP_PRIVATE_KEY_PATH": "/dev/null",
    "HEALTH_CHECK_INTERVAL": "60",
    "INTERNAL_API_KEY": "test-internal-key",
    "LK_DOMAIN": "https://lk.test.example.com",
    "LK_JWT_SECRET": "test-lk-jwt-secret",
    "OPENAI_API_KEY": "sk-test-not-real",
    "ORCHESTRATOR_HOSTNAME": "localhost",
    "REDIS_URL": "redis://127.0.0.1:6379/0",
    "REGISTRY_PASSWORD": "test",
    "REGISTRY_USER": "test",
    "SECRETS_ENCRYPTION_KEY": "wHhIQWmPfLt60oHdxzbQhY1ZKnUon12e5_SuZ33xDxc=",
    "TELEGRAM_BOT_TOKEN": "0000000000:test-token",
    "WORKER_API_URL": "http://127.0.0.1:8000",
    "WORKER_BROKER_INTERNAL_TOKEN": "test-worker-broker-internal-token",
    "WORKER_MANAGER_URL": "http://127.0.0.1:8001",
    "WORKER_REDIS_URL": "redis://127.0.0.1:6379/0",
}


def run(command: list[str]) -> None:
    subprocess.run(command, check=True, cwd=ROOT)


def capture(command: list[str]) -> str:
    return subprocess.run(command, check=True, cwd=ROOT, capture_output=True, text=True).stdout


def listed_service_images() -> list[tuple[str, str, str]]:
    """(image, dockerfile, context) of every image of the release chain's one list."""
    listing = capture(
        [
            "bash",
            "-c",
            'source "$1"; printf "%s\\n" "${SERVICE_IMAGES[@]}"',
            "_",
            str(SERVICE_IMAGE_LIST),
        ]
    )
    entries = []
    for line in listing.splitlines():
        image, dockerfile, context = line.split()
        entries.append((image, dockerfile, context))
    return entries


def npm_locked(dockerfile: str, context: str) -> bool:
    """A frontend image: ``npm ci`` installs its package-lock.json and fails on any drift."""
    return (ROOT / context / "package-lock.json").is_file() and "npm ci" in (
        ROOT / dockerfile
    ).read_text()


def assert_every_listed_image_is_locked() -> None:
    """Fail before Docker work when a released image would escape the lock check.

    Every image of infra/scripts/service-images.sh is either a Python image this script
    builds and checks against its requirements.lock, or a frontend whose ``npm ci``
    enforces its package-lock.json. Anything else has no lock anybody checks.
    """
    guarded = {service.name: service.dockerfile for service in SERVICE_IMAGES}
    listed_python = {}
    for image, dockerfile, context in listed_service_images():
        lock = ROOT / Path(dockerfile).parent / service_image_locks.LOCK
        if lock.is_file():
            listed_python[image] = dockerfile
        elif not npm_locked(dockerfile, context):
            raise RuntimeError(
                f"{image} ({dockerfile}) has neither a requirements.lock nor an npm ci "
                "package-lock.json, so nothing checks what it installs"
            )
    if listed_python != guarded:
        raise RuntimeError(
            f"The Python images of {SERVICE_IMAGE_LIST.relative_to(ROOT)} "
            f"{sorted(listed_python.items())} are not the images this check builds "
            f"{sorted(guarded.items())}"
        )


def command_module(command: str | list[str]) -> str | None:
    """Read a Python module from a Compose ``python -m`` command."""
    tokens = shlex.split(command) if isinstance(command, str) else command
    if not all(isinstance(token, str) for token in tokens):
        raise RuntimeError(f"Compose command is not a string list: {command!r}")
    if "-m" not in tokens:
        return None
    module_position = tokens.index("-m") + 1
    if module_position == len(tokens):
        raise RuntimeError(f"Compose command has -m without a module: {command!r}")
    return tokens[module_position]


def compose_command_modules() -> dict[str, tuple[str, ...]]:
    """Read every guarded image's explicit Python entry module from Compose."""
    compose = yaml.safe_load(COMPOSE.read_text())
    services = compose.get("services") if isinstance(compose, dict) else None
    if not isinstance(services, dict):
        raise RuntimeError(f"{COMPOSE} has no services mapping")

    guarded_images = {service.name for service in SERVICE_IMAGES}
    modules: dict[str, list[str]] = {name: [] for name in guarded_images}
    for service_name, service in services.items():
        if not isinstance(service, dict):
            continue
        command = service.get("command")
        if command is None:
            continue
        module = command_module(command)
        if module is None or not module.startswith("src."):
            continue
        image = service.get("image")
        if not isinstance(image, str) or not image.startswith("codegen-orchestrator/"):
            raise RuntimeError(
                f"Compose service {service_name} starts {module} without a guarded image"
            )
        image_name = image.removeprefix("codegen-orchestrator/").partition(":")[0]
        if image_name not in guarded_images:
            raise RuntimeError(
                f"Compose service {service_name} starts {module} from unguarded image {image_name}"
            )
        modules[image_name].append(module)

    return {name: tuple(sorted(set(image_modules))) for name, image_modules in modules.items()}


def modules_for(image_name: str) -> tuple[str, ...]:
    """The default, Compose, and lazy entry modules one image must import."""
    image = next((service for service in SERVICE_IMAGES if service.name == image_name), None)
    if image is None:
        raise RuntimeError(f"Unknown guarded image {image_name}")
    modules = (
        image.default_module,
        *compose_command_modules()[image_name],
        *EXTRA_IMPORT_MODULES.get(image_name, ()),
    )
    return tuple(dict.fromkeys(modules))


def assert_compose_modules_covered(coverage: dict[str, tuple[str, ...]]) -> None:
    """Fail before Docker work when a Compose service module would be missed."""
    compose = yaml.safe_load(COMPOSE.read_text())
    services = compose.get("services") if isinstance(compose, dict) else None
    if not isinstance(services, dict):
        raise RuntimeError(f"{COMPOSE} has no services mapping")

    for service_name, service in services.items():
        if not isinstance(service, dict) or service.get("command") is None:
            continue
        module = command_module(service["command"])
        if module is None or not module.startswith("src."):
            continue
        image = service.get("image")
        image_name = image.removeprefix("codegen-orchestrator/").partition(":")[0]
        if module not in coverage.get(image_name, ()):
            raise RuntimeError(
                f"Compose service {service_name} starts {module}, but its image is not covered"
            )


def check_service_image(service: ServiceImage, modules: tuple[str, ...]) -> tuple[float, list[str]]:
    """Build one image, import its modules in it, and return its lock problems."""
    started_at = time.monotonic()
    run(
        [
            "docker",
            "buildx",
            "build",
            "--load",
            "--tag",
            service.tag,
            "--file",
            service.dockerfile,
            ".",
        ]
    )
    environment = [
        item for name, value in IMPORT_ENV.items() for item in ("--env", f"{name}={value}")
    ]
    run(
        [
            "docker",
            "run",
            "--rm",
            "--entrypoint",
            "python",
            *environment,
            service.tag,
            "-c",
            "; ".join(f"import {module}" for module in modules),
        ]
    )
    probe = capture(
        [
            "docker",
            "run",
            "--rm",
            "--entrypoint",
            "python",
            service.tag,
            "-c",
            service_image_locks.PROBE,
        ]
    )
    problems = service_image_locks.check_image(
        service.name, service.lock.read_text(), service.pyproject.read_text(), probe
    )
    return time.monotonic() - started_at, problems


def main() -> None:
    started_at = time.monotonic()
    coverage = {service.name: modules_for(service.name) for service in SERVICE_IMAGES}
    assert_compose_modules_covered(coverage)
    assert_every_listed_image_is_locked()
    drifted = []
    for service in SERVICE_IMAGES:
        modules = coverage[service.name]
        duration, problems = check_service_image(service, modules)
        verdict = "matches its lock" if not problems else f"{len(problems)} lock problem(s)"
        print(
            f"{service.name}: imported {', '.join(modules)} from its image, {verdict}, "
            f"in {duration:.1f}s"
        )
        drifted += problems
    print(f"Checked {len(SERVICE_IMAGES)} service images in {time.monotonic() - started_at:.1f}s")
    if drifted:
        print("Service images whose installed dependencies are not their lock:", file=sys.stderr)
        for problem in drifted:
            print(f"  {problem}", file=sys.stderr)
        print("Regenerate the locks with `make lock-deps` and rebuild.", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
