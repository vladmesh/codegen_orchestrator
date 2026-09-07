"""Canonical service-template compatibility smoke harness."""

from __future__ import annotations

import argparse
import ast
from dataclasses import dataclass, field
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from uuid import uuid4

import yaml

ROOT = Path(__file__).resolve().parents[3]
# `make template-compat` runs this file by path, which puts this directory on sys.path and
# not the repository root, so the pin module could not be found on its own.
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.template_pin import SYSTEM_CONFIGS_PATH, load_template_pin  # noqa: E402

SYSTEM_CONFIG = SYSTEM_CONFIGS_PATH
COMPOSE_LABEL = "com.docker.compose.project"
COMMAND_TIMEOUT_SECONDS = 20 * 60
SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$", re.IGNORECASE)
# The one package the kit publishes today. Nothing publishes its wheel, so the recipe
# builds it from the kit source at the ref the product is pinned to.
KIT_PACKAGE = "reminders"
KIT_PACKAGE_DISTRIBUTION = "codegen-kit-reminders"
KIT_PACKAGE_WHEEL_GLOB = "codegen_kit_reminders-*.whl"
ACTIVE_PACKAGES_RELPATH = Path("codegen_kit/_active_packages.py")
BACKEND_MANIFEST_RELPATH = Path("services/backend/manifest.yaml")
# The orchestrator module central QA establishes a deployment's packages with.
# It is loaded from its own file so this smoke exercises the shipped reader
# without importing the LangGraph service's settings.
QA_PACKAGE_READER = ROOT / "services/langgraph/src/agents/qa/packages.py"
# A URL for the facts the reader renders; nothing is called on it here.
QA_FACTS_URL = "http://stage5-package.test:8000"


def _set_standard_umask() -> None:
    os.umask(0o022)


@dataclass(frozen=True)
class TemplateRevision:
    source: str
    ref: str


class CommandTimeout(RuntimeError):
    """A smoke phase exceeded its explicit command timeout."""


def load_production_template(path: Path = SYSTEM_CONFIG) -> TemplateRevision:
    """Read the production source and pin from the one place they are defined."""
    pin = load_template_pin(path)
    return TemplateRevision(source=pin.source, ref=pin.ref)


def read_active_packages(product: Path) -> list[dict[str, str]]:
    """Read the generated active-package contract the product runtime is pinned to."""
    module = ast.parse((product / ACTIVE_PACKAGES_RELPATH).read_text())
    for node in module.body:
        target = node.target if isinstance(node, ast.AnnAssign) else None
        if isinstance(target, ast.Name) and target.id == "ACTIVE_PACKAGES" and node.value:
            return ast.literal_eval(node.value)
    raise RuntimeError(f"{product / ACTIVE_PACKAGES_RELPATH} declares no ACTIVE_PACKAGES")


def load_qa_package_reader():
    """Load the reader central QA uses on a deployment, from its own source."""
    spec = importlib.util.spec_from_file_location("qa_packages_under_test", QA_PACKAGE_READER)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"{QA_PACKAGE_READER} is not importable")
    module = importlib.util.module_from_spec(spec)
    # A dataclass in a module compiled with postponed annotations resolves them
    # through `sys.modules`, so the module has to be registered before it runs.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def read_listed_packages(product: Path) -> list[str]:
    """Read the package allowlist of the product's backend manifest."""
    manifest = yaml.safe_load((product / BACKEND_MANIFEST_RELPATH).read_text())
    return manifest["packages"]


@dataclass(frozen=True)
class Stage5Smoke:
    """Run one requested template revision's full worker-mode contract."""

    workspace: Path
    compose_project_name: str
    template: TemplateRevision
    artifact: Path
    command_timeout: int = COMMAND_TIMEOUT_SECONDS
    installed_packages: list[dict[str, str]] = field(default_factory=list)

    @classmethod
    def create(
        cls,
        temporary_root: Path,
        *,
        source: str,
        ref: str,
        artifact: Path | None = None,
    ) -> Stage5Smoke:
        run_id = uuid4().hex
        return cls(
            workspace=temporary_root / f"stage5-template-{run_id}",
            compose_project_name=f"codegen_stage5_{run_id}",
            template=TemplateRevision(source=source, ref=ref),
            artifact=artifact or temporary_root / "template-compat-result.json",
        )

    @property
    def package_workspace(self) -> Path:
        """Root of the throwaway render the package install recipe is proven on."""
        return self.workspace.parent / f"{self.workspace.name}-package"

    def run(self) -> str:
        resolved_commit: str | None = None
        error: str | None = None
        self.workspace.mkdir(parents=True)
        try:
            resolved_commit = self._resolve_remote_ref(self.template.ref)
            self._run_copier(resolved_commit)
            self._read_resolved_commit(resolved_commit)
            self._run_make("setup")
            self._run_make("lint")
            self._run_make("tests")
            self._make_workspace_readable()
            self._run_worker_start()
            self._run_make(
                "smoke-probe",
                "SMOKE_RUNNER=backend",
                "SMOKE_URL=http://backend:8000/health",
            )
            self._exercise_generated_access_lifecycle()
            self._prove_kit_package_install(resolved_commit)
            return resolved_commit
        except Exception as caught:
            error = str(caught)
            raise
        finally:
            try:
                self.cleanup()
            except Exception as cleanup_error:
                error = f"{error}\ncleanup: {cleanup_error}" if error else str(cleanup_error)
                raise
            finally:
                self._write_artifact(resolved_commit, error)

    def cleanup(self) -> None:
        compose_file = self.workspace / "infra" / "compose.base.yml"
        if compose_file.exists():
            self._run(
                [
                    "docker",
                    "compose",
                    "-p",
                    self.compose_project_name,
                    "-f",
                    "infra/compose.base.yml",
                    "-f",
                    "infra/compose.dev.yml",
                    "down",
                    "--volumes",
                    "--remove-orphans",
                ],
                cwd=self.workspace,
                phase="cleanup",
            )
        self._assert_no_compose_resources()
        shutil.rmtree(self.package_workspace, ignore_errors=True)
        shutil.rmtree(self.workspace, ignore_errors=True)

    def _run_copier(
        self,
        resolved_commit: str,
        destination: Path | None = None,
        *,
        project_name: str = "stage5-smoke",
        modules: str = "backend,tg_bot",
        phase: str = "scaffold",
    ) -> None:
        self._run(
            [
                "copier",
                "copy",
                "--defaults",
                # Same admission as the scaffolder: the source is bounded by
                # ServiceTemplateSource to owner-controlled repositories.
                "--trust",
                f"--vcs-ref={resolved_commit}",
                "--data",
                f"project_name={project_name}",
                "--data",
                f"modules={modules}",
                "--data",
                "task_description=deterministic local contract smoke",
                self.template.source,
                str(destination or self.workspace),
            ],
            phase=phase,
        )

    def _read_resolved_commit(self, expected_commit: str) -> str:
        answers = yaml.safe_load((self.workspace / ".copier-answers.yml").read_text())
        commit = answers.get("_commit") if isinstance(answers, dict) else None
        if not isinstance(commit, str) or not self._recorded_ref_matches(commit, expected_commit):
            raise RuntimeError(
                f"Copier resolved unexpected commit: expected={expected_commit!r}, "
                f"recorded={commit!r}"
            )
        return expected_commit

    def _recorded_ref_matches(self, recorded: str, resolved: str) -> bool:
        if SHA_PATTERN.fullmatch(recorded):
            return recorded.lower() == resolved
        if recorded == self.template.ref:
            return True
        describe_match = re.search(r"-g([0-9a-f]{7,40})$", recorded, re.IGNORECASE)
        return bool(
            SHA_PATTERN.fullmatch(self.template.ref)
            and describe_match
            and resolved.startswith(describe_match.group(1).lower())
        )

    def _resolve_remote_ref(self, ref: str) -> str:
        with tempfile.TemporaryDirectory(
            prefix="template-ref-", dir=self.workspace.parent
        ) as repository:
            self._run(["git", "init", "--bare", repository], phase="initialize ref resolver")
            self._run(
                [
                    "git",
                    "-C",
                    repository,
                    "fetch",
                    "--depth=1",
                    self._git_source(),
                    ref,
                ],
                phase="fetch template ref",
            )
            result = self._run(
                ["git", "-C", repository, "rev-parse", "FETCH_HEAD^{commit}"],
                phase="resolve template ref",
            )
        resolved = result.stdout.strip().lower()
        if not SHA_PATTERN.fullmatch(resolved):
            raise RuntimeError(
                f"template ref cannot be resolved to a commit SHA: {self.template.source}@{ref}"
            )
        if SHA_PATTERN.fullmatch(ref) and resolved != ref.lower():
            raise RuntimeError(
                f"template commit mismatch: requested={ref.lower()}, resolved={resolved}"
            )
        return resolved

    def _git_source(self) -> str:
        prefix = "gh:"
        if self.template.source.startswith(prefix):
            return f"https://github.com/{self.template.source.removeprefix(prefix)}.git"
        return self.template.source

    def _write_artifact(self, resolved_commit: str | None, error: str | None) -> None:
        self.artifact.parent.mkdir(parents=True, exist_ok=True)
        self.artifact.write_text(
            json.dumps(
                {
                    "requested_source": self.template.source,
                    "requested_ref": self.template.ref,
                    "resolved_commit": resolved_commit,
                    "outcome": "failed" if error else "passed",
                    "error": error,
                    "compose_project_name": self.compose_project_name,
                    "installed_packages": list(self.installed_packages),
                },
                indent=2,
            )
            + "\n"
        )

    def _run_make(self, target: str, *variables: str, cwd: Path | None = None) -> None:
        self._run(["make", target, *variables], cwd=cwd or self.workspace, phase=target)

    def _run_worker_start(self) -> None:
        try:
            self._run_make("worker-start")
        except RuntimeError as error:
            logs = self._run(
                [
                    "docker",
                    "compose",
                    "-p",
                    self.compose_project_name,
                    "-f",
                    "infra/compose.base.yml",
                    "-f",
                    "infra/compose.dev.yml",
                    "logs",
                    "--no-color",
                ],
                cwd=self.workspace,
                check=False,
                phase="worker-start logs",
            )
            raise RuntimeError(f"{error}\ncompose logs:\n{logs.stdout}\n{logs.stderr}") from error

    def _exercise_generated_access_lifecycle(self) -> None:
        """Prove generated capability admission and bot denial against this stack."""
        self._run_service_python(
            "backend",
            """
import json
import os
import urllib.request

identity = {"channel": "telegram", "external_id": "8202532144"}
base_url = "http://backend:8000/users"
capability = os.environ["USERS_GRANT_CAPABILITY"]

def request(path, *, method="GET", payload=None, privileged=False):
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(f"{base_url}{path}", data=data, method=method)
    if payload is not None:
        request.add_header("Content-Type", "application/json")
    if privileged:
        request.add_header("X-Grant-Capability", capability)
    with urllib.request.urlopen(request, timeout=10) as response:
        assert response.status == 200
        return json.load(response)

assert request("/grant", method="POST", payload=identity, privileged=True)["status"] == "active"
assert request("/access?channel=telegram&external_id=8202532144")["status"] == "active"
assert request("/revoke", method="POST", payload=identity, privileged=True)["status"] == "inactive"
assert request("/access?channel=telegram&external_id=8202532144")["status"] == "inactive"
""",
            phase="generated access grant and revoke",
        )
        self._run_service_python(
            "tg_bot",
            """
import asyncio

from telegram.ext import ApplicationHandlerStop

from services.tg_bot.src.main import enforce_access

class User:
    id = 8202532144

class Update:
    effective_user = User()

async def verify_denial():
    try:
        await enforce_access(Update(), object())
    except ApplicationHandlerStop:
        return
    raise AssertionError("revoked Telegram identity reached bot admission")

asyncio.run(verify_denial())
""",
            phase="generated bot denial after revoke",
        )

    def _prove_kit_package_install(self, resolved_commit: str) -> None:
        """Install a kit package the way an engineering worker has to, and check the contract.

        Nothing publishes the package wheel, so the recipe builds it from the kit source at
        the ref the product is pinned to. The proof runs on its own render: the smoke's own
        product stays package-free, which is what every product without a package looks like.
        """
        product = self.package_workspace / "product"
        product.mkdir(parents=True)
        self._run_copier(
            resolved_commit,
            product,
            project_name="stage5-package",
            modules="backend",
            phase="scaffold the package product",
        )
        self._run_make("setup", cwd=product)
        if read_active_packages(product) or read_listed_packages(product):
            raise AssertionError(
                "a freshly rendered product must declare no packages, but it declares "
                f"listed={read_listed_packages(product)} "
                f"generated={read_active_packages(product)}"
            )

        wheel = self._build_package_wheel(resolved_commit)
        self._run(
            [str(product / ".venv/bin/kit"), "add", KIT_PACKAGE, "--wheel", str(wheel)],
            cwd=product,
            phase=f"kit add {KIT_PACKAGE}",
        )

        identities = read_active_packages(product)
        self.installed_packages.extend(identities)
        if [identity["name"] for identity in identities] != [KIT_PACKAGE]:
            raise AssertionError(f"generated contract does not record the package: {identities}")
        if read_listed_packages(product) != [KIT_PACKAGE]:
            raise AssertionError(
                f"manifest allowlist does not list the package: {read_listed_packages(product)}"
            )
        installed_wheel = product / "services/backend/packages" / wheel.name
        if not installed_wheel.is_file():
            raise AssertionError(f"kit add copied no wheel to {installed_wheel}")
        self._prove_central_qa_reads_the_generated_contract(product)

    def _prove_central_qa_reads_the_generated_contract(self, product: Path) -> None:
        """Run central QA's package reader over the real generated artifacts.

        The failure mode this defends against is a test that exercises a
        hand-built replica instead of the generated artifact. These three files
        were written by the kit's own generators through `kit add` moments ago,
        and what reads them here is the module the QA runner reads a deployment
        with — so the shape QA depends on is proven against a real render.
        """
        qa = load_qa_package_reader()
        packages = qa.parse_active_packages((product / qa.ACTIVE_PACKAGE_CONTRACT).read_text())
        listed = qa.parse_listed_packages((product / qa.BACKEND_MANIFEST).read_text())
        owners = qa.parse_job_owners((product / qa.GENERATED_JOB_REGISTRY).read_text())
        activation = qa.PackageActivation(packages=packages, listed=listed, jobs=owners)
        if activation.names != [KIT_PACKAGE] or listed != (KIT_PACKAGE,):
            raise AssertionError(
                f"central QA reads {activation.names} listed={listed} off the generated "
                f"contract of a product that installed {KIT_PACKAGE}"
            )
        if not activation.package_jobs:
            raise AssertionError(
                f"central QA reads no package-declared job off {qa.GENERATED_JOB_REGISTRY}: "
                f"{owners}"
            )
        facts = "\n".join(
            qa.active_package_facts(
                activation,
                deployed_url=QA_FACTS_URL,
                fireable_behaviours=tuple(activation.package_jobs),
            )
        )
        for expected in (KIT_PACKAGE, QA_FACTS_URL, "refuses to boot", *activation.package_jobs):
            if expected not in facts:
                raise AssertionError(f"the QA package facts do not state {expected!r}: {facts}")

    def _build_package_wheel(self, resolved_commit: str) -> Path:
        """Build the package wheel from the kit source at the ref the product is pinned to."""
        kit = self.package_workspace / "kit"
        wheels = self.package_workspace / "wheels"
        self._run(
            ["git", "clone", "--quiet", self._git_source(), str(kit)],
            phase="clone the kit source",
        )
        self._run(
            ["git", "-C", str(kit), "checkout", "--quiet", resolved_commit],
            phase="check out the pinned kit ref",
        )
        self._run(
            [
                "uv",
                "build",
                "--wheel",
                str(kit / "packages" / KIT_PACKAGE_DISTRIBUTION),
                "--out-dir",
                str(wheels),
            ],
            cwd=kit,
            phase="build the package wheel",
        )
        built = sorted(wheels.glob(KIT_PACKAGE_WHEEL_GLOB))
        if len(built) != 1:
            raise AssertionError(f"expected exactly one {KIT_PACKAGE_DISTRIBUTION} wheel: {built}")
        return built[0]

    def _run_service_python(self, service: str, source: str, *, phase: str) -> None:
        self._run(
            [
                "docker",
                "compose",
                "-p",
                self.compose_project_name,
                "-f",
                "infra/compose.base.yml",
                "-f",
                "infra/compose.dev.yml",
                "exec",
                "-T",
                service,
                "python",
                "-c",
                source,
            ],
            cwd=self.workspace,
            phase=phase,
        )

    def _make_workspace_readable(self) -> None:
        for path in (self.workspace, *self.workspace.rglob("*")):
            if path.is_symlink():
                continue
            mode = path.stat().st_mode
            readable_mode = mode | stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH
            if path.is_dir():
                readable_mode |= stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
            path.chmod(readable_mode)

    def _assert_no_compose_resources(self) -> None:
        for resource, args in (
            ("containers", ["docker", "ps", "-aq"]),
            ("networks", ["docker", "network", "ls", "-q"]),
            ("volumes", ["docker", "volume", "ls", "-q"]),
        ):
            result = self._run(
                [*args, "--filter", f"label={COMPOSE_LABEL}={self.compose_project_name}"],
                phase=f"verify cleanup {resource}",
            )
            if result.stdout.strip():
                raise AssertionError(f"Stage 5 smoke left {resource}: {result.stdout.strip()}")

    def _run(
        self,
        command: list[str],
        *,
        cwd: Path | None = None,
        check: bool = True,
        phase: str = "command",
    ) -> subprocess.CompletedProcess[str]:
        workspace_owner = self.workspace.parent.stat()
        environment = os.environ | {
            "COMPOSE_PROJECT_NAME": self.compose_project_name,
            "HOST_UID": str(workspace_owner.st_uid),
            "HOST_GID": str(workspace_owner.st_gid),
        }
        environment.pop("VIRTUAL_ENV", None)
        try:
            result = subprocess.run(
                command,
                cwd=cwd,
                env=environment,
                check=False,
                text=True,
                capture_output=True,
                preexec_fn=_set_standard_umask,
                timeout=self.command_timeout,
            )
        except subprocess.TimeoutExpired as error:
            raise CommandTimeout(
                f"Phase {phase} timed out after {self.command_timeout}s: {' '.join(command)}"
            ) from error
        if check and result.returncode:
            raise RuntimeError(
                f"Phase {phase} failed ({result.returncode}): {' '.join(command)}\n"
                f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
            )
        return result


def main() -> None:
    production = load_production_template()
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", default=production.source)
    parser.add_argument("--ref", default=production.ref)
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument("--workspace-root", type=Path, required=True)
    args = parser.parse_args()
    smoke = Stage5Smoke.create(
        args.workspace_root, source=args.source, ref=args.ref, artifact=args.artifact
    )
    resolved = smoke.run()
    print(
        f"template compatibility passed: requested={args.source}@{args.ref} "
        f"resolved={resolved} artifact={args.artifact}"
    )


if __name__ == "__main__":
    main()
