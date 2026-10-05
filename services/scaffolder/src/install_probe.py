"""Read-only fixed probe, executed by the product's isolated tooling interpreter.

This file has no service imports. Every kit import comes from the product's own
locked environment; nothing installs host tooling or patches application files.
"""

import ast
from dataclasses import asdict
import hashlib
from importlib import metadata
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import tomllib

import framework
from framework.binding_product import (
    binding_files,
    default_binding_resource,
    product_core_version,
    require_binding_product,
    validate_product_bindings,
)
from framework.bindings import load_binding, validate_binding
from framework.package_source import (
    DEFAULT_CATALOG_REF,
    DEFAULT_CATALOG_SOURCE,
    fetch_package_source,
    read_catalog,
)
from framework.spec.loader import load_specs
from framework.spec.packages import load_package_manifest
import yaml


def digest(data):
    return hashlib.sha256(data).hexdigest()


def installed(root, service, distribution):
    environment = root / f"services/{service}/.venv"
    code = (
        "import sys, json; from importlib.metadata import distribution; "
        "d=distribution(sys.argv[1]); print(json.dumps({'prefix':sys.prefix, "
        "'version':d.version, 'direct_url':d.read_text('direct_url.json')}))"
    )
    result = subprocess.run(
        [str(environment / "bin/python"), "-I", "-c", code, distribution],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    evidence = json.loads(result.stdout)
    if Path(evidence["prefix"]).resolve() != environment.resolve():
        raise ValueError("environment_unowned: service interpreter prefix")
    return evidence


def probe(mode, payload, ref):  # noqa: C901, PLR0912, PLR0915  # cross-check actual product provenance, binding and activation
    root = Path.cwd().resolve()
    environment = (root / ".venv").resolve()
    if (
        not environment.is_relative_to(root)
        or Path(sys.prefix).resolve() != environment
        or not Path(framework.__file__).resolve().is_relative_to(environment)
    ):
        raise ValueError("tooling_unowned: use the product's own tooling interpreter")
    answers = yaml.safe_load((root / ".copier-answers.yml").read_text())
    if answers["_src_path"] != "gh:vladmesh/codegen-product-kit" or answers["_commit"] != ref:
        raise ValueError("template_incompatible: reviewed native Copier upgrade required")
    if set(answers["modules"].split(",")) != {"backend", "tg_bot"}:
        raise ValueError("modules_missing: backend,tg_bot required")
    tooling = metadata.distribution("codegen-kit-tooling")
    direct = json.loads(tooling.read_text("direct_url.json"))
    if direct["url"] != "https://github.com/vladmesh/codegen-product-kit.git":
        raise ValueError("tooling_unowned: installed source must be the published kit")
    revision = direct["vcs_info"]["commit_id"]
    root_project = tomllib.loads((root / "pyproject.toml").read_text())
    root_lock = tomllib.loads((root / "uv.lock").read_text())
    locked = next(item for item in root_lock["package"] if item["name"] == "codegen-kit-tooling")
    if (
        revision != payload["tooling_commit"]
        or revision not in locked["source"]["git"]
        or not any(
            f"codegen-product-kit.git@{revision}" in item
            for item in root_project["project"]["dependencies"]
        )
    ):
        raise ValueError(
            "tooling_incompatible: saved requirement, lock and installed revision disagree"
        )
    core = product_core_version(root)
    if core != payload["core_version"]:
        raise ValueError("core_incompatible: reviewed native Copier upgrade required")
    require_binding_product(root)
    catalog = read_catalog(DEFAULT_CATALOG_SOURCE, DEFAULT_CATALOG_REF)
    catalog_digest = digest(
        json.dumps(
            asdict(catalog), sort_keys=True, default=lambda value: value.model_dump(mode="json")
        ).encode()
    )
    if catalog_digest != payload["catalog_digest"]:
        raise ValueError("catalog_changed: replan against the current catalog")
    package = catalog.get(payload["package"]["name"])
    selected = package.select(core)
    if (
        selected.version != payload["package"]["version"]
        or selected.tag != payload["package"]["tag"]
        or package.distribution != payload["package"]["distribution"]
    ):
        raise ValueError("package_identity_changed")
    git = shutil.which("git")
    if git is None:
        raise ValueError("git_missing: product installation requires git")
    component_sources = []
    with tempfile.TemporaryDirectory(prefix="install-preflight-") as scratch:
        for expected in [payload["package"], *payload["libraries"]]:
            component = catalog.get_component(expected["name"])
            version = next(
                item for item in component.versions if item.version == expected["version"]
            )
            if expected["tag"] != version.tag or component.distribution != expected["distribution"]:
                raise ValueError("component_identity_changed")
            workdir = Path(scratch) / expected["name"]
            workdir.mkdir()
            source = fetch_package_source(DEFAULT_CATALOG_SOURCE, component, version, workdir)
            target = (
                subprocess.check_output(
                    [git, "rev-parse", "FETCH_HEAD^{commit}"], cwd=workdir / "repository"
                )
                .decode()
                .strip()
            )
            tag_object = (
                subprocess.check_output(
                    [git, "rev-parse", "FETCH_HEAD"], cwd=workdir / "repository"
                )
                .decode()
                .strip()
            )
            tree = (
                subprocess.check_output(
                    [git, "rev-parse", f"{target}:{component.path}"], cwd=workdir / "repository"
                )
                .decode()
                .strip()
            )
            component_sources.append(
                {
                    "name": expected["name"],
                    "tag": version.tag,
                    "tag_object": tag_object,
                    "tree": tree,
                    "target": target,
                    "source": DEFAULT_CATALOG_SOURCE,
                }
            )
            if expected is payload["package"]:
                module, resource = payload["binding"]["resource"].split(":", 1)
                resource_path = source / module.replace(".", "/") / resource
                content = resource_path.read_bytes()
                if digest(content) != payload["binding"]["sha256"]:
                    raise ValueError("binding_resource_changed")
                manifest = load_package_manifest(source / module.replace(".", "/") / "package.yaml")
                binding = load_binding(resource_path)
                validate_binding(binding, manifest, catalog)
                existing = root / f"services/tg_bot/bindings/{package.name}.yaml"
                if existing.exists() and existing.read_bytes() != content:
                    raise ValueError("binding_owned: existing product binding differs")
                reserved = {"start", "command"}
                for current in binding_files(root).values():
                    if current.package != package.name:
                        reserved.update(item.command for item in current.commands)
                if reserved.intersection(item.command for item in binding.commands):
                    raise ValueError("binding_conflict: command is already owned")
                tg_manifest_path = root / "services/tg_bot/manifest.yaml"
                schema = None
                if tg_manifest_path.exists():
                    tg_manifest = yaml.safe_load(tg_manifest_path.read_text())
                    schema = tg_manifest["settings_schema"]["properties"].get(binding.timezone.key)
                if schema is not None and schema != {"type": "string", "format": "x-iana-tz"}:
                    raise ValueError("binding_conflict: timezone schema is already owned")
            else:
                # Kit's native target-interpreter admission, never host Python.
                from framework.cli import _library_python_version

                if component.select(_library_python_version(root)).version != expected["version"]:
                    raise ValueError("library_incompatible: target Python requires replanning")
    evidence = {
        "answers": {key: answers[key] for key in ("_src_path", "_commit", "modules")},
        "tooling": direct,
        "tooling_import": framework.__file__,
        "prefix": sys.prefix,
        "core": core,
        "component_sources": component_sources,
    }
    if mode == "readback":
        specs = load_specs(root)
        active = next(item for item in specs.packages if item.name == package.name)
        if active.manifest.version != selected.version:
            raise ValueError("installed_package_version_mismatch")
        resource = default_binding_resource(active)
        target = root / f"services/tg_bot/bindings/{package.name}.yaml"
        if (
            target.read_bytes() != resource.read_bytes()
            or digest(target.read_bytes()) != payload["binding"]["sha256"]
        ):
            raise ValueError("installed_binding_resource_mismatch")
        validate_product_bindings(root, specs, catalog=catalog)
        tree = ast.parse((root / "codegen_kit/_active_packages.py").read_text())
        contract = next(
            ast.literal_eval(item.value)
            for item in tree.body
            if isinstance(item, ast.AnnAssign)
            and isinstance(item.target, ast.Name)
            and item.target.id == "ACTIVE_PACKAGES"
        )
        if not any(
            item["name"] == package.name
            and item["version"] == selected.version
            and item["manifest_sha256"] == active.manifest_sha256
            for item in contract
        ):
            raise ValueError("activation_stale: regenerated package contract")
        allowlist = yaml.safe_load((root / "services/backend/manifest.yaml").read_text())[
            "packages"
        ]
        if package.name not in allowlist:
            raise ValueError("allowlist_missing")
        evidence["active_packages"] = contract
        evidence["allowlist"] = allowlist
        evidence["distributions"] = {}
        for expected in [payload["package"], *payload["libraries"]]:
            service = "backend" if expected is payload["package"] else "tg_bot"
            found = installed(root, service, expected["distribution"])
            if found["version"] != expected["version"]:
                raise ValueError("installed_distribution_mismatch")
            evidence["distributions"][expected["name"]] = found
        evidence["binding_sha256"] = digest(target.read_bytes())
    return evidence


if __name__ == "__main__":
    sys.stdout.write(json.dumps(probe(sys.argv[1], json.loads(sys.argv[2]), sys.argv[3])) + "\n")
