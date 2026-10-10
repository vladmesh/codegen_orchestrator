"""The kit's pinned runner proof, planned through this orchestrator's activated snapshot.

The kit's harness (`tests/runner/fresh_product.py` at the pinned kit commit) proves a fresh
backend,tg_bot product end to end: the orchestrator's real install executor, product CI and
drift, the cold environment regression, pushed and run image digests, the pinned platform's
real auth and Caddy with key negatives, the fixture reader, timer post delivery, the
coexistence reminder and the RU/EN causal probes. Every one of those stages runs here
unchanged, from the kit checkout.

The harness's own selection reads the kit's default branch (`catalog_url(source, 'HEAD')`),
which this orchestrator no longer plans from: a payload read at a moving ref names no
catalog commit and is refused (`catalog_unpinned`). This narrow adapter replaces exactly
two things, and says so in the evidence:

* catalog mode `activated_snapshot` — the catalog evidence is the commit in
  `shared/catalog_activation.yaml`, fetched by git at that commit and checked against its
  raw and semantic digests before and after the installs; the real remote's HEAD is
  recorded beside it, because it may differ and must not matter;
* the selection — `tests/runner/production_plan.py` runs the production path against the
  orchestrator API built from this checkout and its database: preview, brief, confirmation,
  stored plan and the Architect's first planning attempt. The INSTALL task payloads read
  back from the API are what the harness installs.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import secrets
import sys
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
CATALOG_MODE = "activated_snapshot"
CATALOG_FILE = "packages/catalog.yaml"
ACTIVATED_TRANSPORT = (
    "none: the activated commit of the real kit repository and its published tags, read by "
    "git at that commit; no fixture, no URL rewrite and no default branch"
)


def _harness(kit: Path):
    sys.path.insert(0, str(kit / "tests/runner"))
    import fresh_product  # noqa: PLC0415 - the pinned kit checkout's harness
    import support  # noqa: PLC0415

    return fresh_product, support


def build_runner(args: argparse.Namespace):  # noqa: C901 - one subclass, defined over the harness
    fresh_product, support = _harness(args.kit_dir.resolve())
    activation = yaml.safe_load(
        (args.orchestrator_dir / "shared/catalog_activation.yaml").read_text()
    )
    fresh_product.RELEASE_TRANSPORTS[CATALOG_MODE] = ACTIVATED_TRANSPORT

    class ActivatedSnapshotRunner(fresh_product.Runner):
        production: dict[str, Any]

        def activated_catalog(self, label: str) -> dict[str, str]:
            """The catalog at the activated commit, read by git from the real remote."""
            target = self.work / f"activated-{label}"
            target.mkdir()
            self.git("init", "-q", cwd=target, label=f"activated catalog ({label})")
            self.git(
                "fetch",
                "-q",
                "--depth=1",
                "--no-tags",
                fresh_product.KIT_REPOSITORY,
                activation["commit"],
                cwd=target,
                label=f"fetch the activated commit ({label})",
            )
            data = self.run(
                ["git", "-c", "core.hooksPath=/dev/null", "show", f"FETCH_HEAD:{CATALOG_FILE}"],
                cwd=target,
                label=f"activated catalog bytes ({label})",
            ).stdout.encode()
            found = {
                "commit": self.git("rev-parse", "FETCH_HEAD", cwd=target, label="activated commit"),
                "catalog_sha256": hashlib.sha256(data).hexdigest(),
                "catalog_digest": support.catalog_digest(data.decode()),
            }
            expected = {key: activation[key] for key in found}
            if found != expected:
                raise fresh_product.ProofError(f"activated commit holds {found}, not {expected}")
            return found

        def activated_snapshot(self) -> None:
            catalog = self.activated_catalog("before")
            self.evidence["catalog"] = {
                "mode": CATALOG_MODE,
                "prospective": False,
                "note": (
                    "the orchestrator's activated immutable catalog snapshot "
                    "(shared/catalog_activation.yaml), not the kit's default branch"
                ),
                "ref": activation["commit"],
                "source": fresh_product.KIT_REPOSITORY,
                "activation": activation,
                "remote_head_at_start": self.remote_head_catalog("head-at-start"),
            } | catalog
            self.production = self.production_plan()
            self.evidence["production_plan"] = self.production

        def production_plan(self) -> dict[str, Any]:
            orchestrator = self.orchestrator
            key = secrets.token_hex(16)
            self.secrets.add(key)
            port = str(fresh_product.free_port())
            compose = [
                "docker",
                "compose",
                "-p",
                f"{self.prefix}-orchestrator",
                "-f",
                str(orchestrator / "tests/runner/compose.orchestrator.yml"),
            ]
            env = self.clean_env(RUNNER_INTERNAL_API_KEY=key, RUNNER_API_PORT=port)
            self.cleanup_later("orchestrator API", [*compose, "down", "-v"], env)
            self.run(
                [*compose, "up", "-d", "--build", "--wait"],
                cwd=orchestrator,
                env=env,
                label="orchestrator API image, database and Redis",
            )
            output = self.work / "production-plan.json"
            self.run(
                [
                    sys.executable,
                    str(orchestrator / "tests/runner/production_plan.py"),
                    "--packages",
                    ",".join(self.packages),
                    "--output",
                    str(output),
                ],
                cwd=orchestrator,
                env=self.clean_env(
                    PYTHONPATH=f"{orchestrator / 'services/langgraph'}:{orchestrator}",
                    API_BASE_URL=f"http://127.0.0.1:{port}",
                    INTERNAL_API_KEY=key,
                    REDIS_URL="redis://127.0.0.1:9/0",
                    DEFAULT_AGENT_TYPE="claude",
                ),
                label="production preview, brief, stored plan and first planning attempt",
            )
            production = json.loads(output.read_text())
            if production["activation"] != activation:
                raise fresh_product.ProofError("the production path ran another activation")
            if production["model_channels"]:
                raise fresh_product.ProofError("a model chose part of the stored plan")
            return production

        def scaffold(self, install: Any) -> Path:
            product = super().scaffold(install)
            self.activated_snapshot()
            return product

        def select_payload(self, name: str) -> dict:
            payload = self.production["install_tasks"][name]["install"]
            if payload["tooling_commit"] != self.sha or payload["package"]["name"] != name:
                raise fresh_product.ProofError("persisted payload names another tooling/package")
            if payload["catalog"]["commit"] != activation["commit"]:
                raise fresh_product.ProofError("persisted payload names another catalog commit")
            version = payload["package"]["version"]
            if name == fresh_product.PACKAGE and version != self.args.package_version:
                raise fresh_product.ProofError(
                    f"the stored plan selected {name} {version}, "
                    f"expected {self.args.package_version}"
                )
            if payload["catalog_digest"] != self.evidence["catalog"]["catalog_digest"]:
                raise fresh_product.ProofError("the stored plan names another catalog")
            stored = {
                item["install"]["package"]["name"]: item["install"]
                for item in self.production["plan"]["capabilities"]
                if item["install"]
            }
            if stored[name] != payload:
                raise fresh_product.ProofError(f"{name}: the task is not the stored closure")
            self.evidence.setdefault("install_payloads", {})[name] = payload
            if name == fresh_product.PACKAGE:
                self.evidence["install_payload"] = payload
            return payload

        def catalog_agreement(self, payloads: dict[str, dict]) -> None:
            super().catalog_agreement(payloads)
            self.evidence["catalog"]["after_install"] = self.activated_catalog("after")

    return ActivatedSnapshotRunner


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    for name in ("kit", "orchestrator", "platform"):
        parser.add_argument(f"--{name}-dir", type=Path, required=True)
        parser.add_argument(f"--{name}-sha", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--package-version", required=True)
    parser.add_argument("--packages", required=True)
    args = parser.parse_args()
    for name in ("kit", "orchestrator", "platform"):
        if not re.fullmatch(r"[0-9a-f]{40}", getattr(args, f"{name}_sha")):
            parser.error(f"--{name}-sha must be a full commit SHA")
    args.proof_mode = "published_release"
    args.catalog_mode = CATALOG_MODE
    args.packages = tuple(args.packages.split(","))
    return build_runner(args)(args).execute()


if __name__ == "__main__":
    sys.exit(main())
