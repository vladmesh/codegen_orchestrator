#!/usr/bin/env python3
"""Build each backend service image and import its runtime module inside it."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import subprocess
import time

ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class ServiceImage:
    name: str
    dockerfile: str
    module: str

    @property
    def tag(self) -> str:
        return f"codegen-orchestrator/{self.name}:entrypoint-import"


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

# These are deliberately inert values. The check imports modules only and must not
# connect to a service, but settings modules validate their required values at import.
IMPORT_ENV = {
    "API_BASE_URL": "http://127.0.0.1:9",
    "BROKER_INTERNAL_TOKEN": "test-worker-broker-internal-token",
    "DATABASE_URL": "postgresql+asyncpg://test:test@127.0.0.1:5432/test",
    "GITHUB_APP_ID": "12345",
    "GITHUB_APP_PRIVATE_KEY_PATH": "/dev/null",
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


def check_service_image(service: ServiceImage) -> float:
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
            f"import {service.module}",
        ]
    )
    return time.monotonic() - started_at


def main() -> None:
    started_at = time.monotonic()
    for service in SERVICE_IMAGES:
        duration = check_service_image(service)
        print(f"{service.name}: imported {service.module} from its image in {duration:.1f}s")
    print(f"Checked {len(SERVICE_IMAGES)} service images in {time.monotonic() - started_at:.1f}s")


if __name__ == "__main__":
    main()
