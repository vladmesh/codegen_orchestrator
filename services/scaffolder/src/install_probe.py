"""Read-only fixed probe, executed by the product's isolated tooling interpreter.

Catalog modes: `provenance` checks the saved source, core, tooling, catalog, tags and
default binding only; `preflight` adds the product-conflict refusals; `readback` verifies
the installed closure. Source modes read a `LocalPackageInstall`: `source-provenance` the
committed package source at the exact HEAD, `source-readback` also its installed wheel.

This file has no service imports. Every kit import comes from the product's own
locked environment; nothing installs host tooling or patches application files.
"""

import ast
from dataclasses import asdict
import hashlib
from importlib import metadata
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
from urllib.parse import urlparse
from urllib.request import url2pathname
import zipfile

import framework
from framework.binding_product import (
    binding_files,
    binding_settings,
    default_binding_resource,
    product_core_version,
    require_binding_product,
    validate_product_bindings,
)
from framework.bindings import load_binding, validate_binding
from framework.cli import _library_python_version, _product_source
from framework.package_source import (
    SourcePackage,
    fetch_package_source,
    read_catalog,
    read_source_package,
    verify_source_wheel,
)
from framework.preflight import metadata_from_source, metadata_from_wheel, require_core
from framework.spec.loader import load_specs
from framework.spec.package_resolution import backend_site_packages
from framework.spec.packages import load_package_manifest
from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet
from packaging.utils import canonicalize_name
from packaging.version import Version
import yaml

#: The one repository a catalog, its package tags and the kit tooling come from.
KIT_REPOSITORY = "https://github.com/vladmesh/codegen-product-kit.git"
#: Each source mode and the `LocalInstallVerification.mode` it reports.
SOURCE_MODES = {"source-provenance": "provenance", "source-readback": "readback"}


def digest(data):
    return hashlib.sha256(data).hexdigest()


def recorded_template_commit_matches(recorded, ref):
    """Copier records a commit pin as itself or in its `git describe` form.

    The same rule as `shared.contracts.template`, repeated here because this probe runs
    in the product's interpreter and imports nothing from the services.
    """
    if recorded == ref:
        return True
    described = re.fullmatch(r".+-\d+-g([0-9a-f]{7,40})", recorded)
    return bool(re.fullmatch(r"[0-9a-f]{40}", ref) and described and ref.startswith(described[1]))


def installed(root, service, distribution):
    environment = root / f"services/{service}/.venv"
    code = (
        "import sys, json; from importlib.metadata import distribution; "
        "d=distribution(sys.argv[1]); print(json.dumps({'prefix':sys.prefix, "
        "'version':d.version, 'direct_url':d.read_text('direct_url.json')}))"
    )
    result = subprocess.run(
        [str(environment / "bin/python"), "-I", "-B", "-c", code, distribution],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    evidence = json.loads(result.stdout)
    if Path(evidence["prefix"]).resolve() != environment.resolve():
        raise ValueError("environment_unowned: service interpreter prefix")
    return evidence


def validate_binding_settings(root, binding):
    manifest_path = root / "services/tg_bot/manifest.yaml"
    if not manifest_path.exists():
        return
    properties = yaml.safe_load(manifest_path.read_text())["settings_schema"]["properties"]
    for key, expected in binding_settings(binding).items():
        schema = properties.get(key)
        if schema is not None and schema != expected:
            raise ValueError(f"binding_conflict: {key} schema is already owned")


def product_trust(root, payload, ref):
    """The probe's own interpreter, template, modules, locked tooling and core, as saved."""
    environment = (root / ".venv").resolve()
    if (
        not environment.is_relative_to(root)
        or Path(sys.prefix).resolve() != environment
        or not Path(framework.__file__).resolve().is_relative_to(environment)
    ):
        raise ValueError("tooling_unowned: use the product's own tooling interpreter")
    answers = yaml.safe_load((root / ".copier-answers.yml").read_text())
    if answers["_src_path"] != "gh:vladmesh/codegen-product-kit" or not (
        recorded_template_commit_matches(str(answers["_commit"]), ref)
    ):
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
    return {
        "answers": {key: answers[key] for key in ("_src_path", "_commit", "modules")},
        "tooling": direct,
        "tooling_import": framework.__file__,
        "prefix": sys.prefix,
        "core": core,
    }


def activation(root, name, version):
    """The native active package, its regenerated contract entry and the backend allowlist."""
    specs = load_specs(root)
    active = next(item for item in specs.packages if item.name == name)
    if active.manifest.version != version:
        raise ValueError("installed_package_version_mismatch")
    tree = ast.parse((root / "codegen_kit/_active_packages.py").read_text())
    contract = next(
        ast.literal_eval(item.value)
        for item in tree.body
        if isinstance(item, ast.AnnAssign)
        and isinstance(item.target, ast.Name)
        and item.target.id == "ACTIVE_PACKAGES"
    )
    if not any(
        item["name"] == name
        and item["version"] == version
        and item["manifest_sha256"] == active.manifest_sha256
        for item in contract
    ):
        raise ValueError("activation_stale: regenerated package contract")
    allowlist = yaml.safe_load((root / "services/backend/manifest.yaml").read_text())["packages"]
    if name not in allowlist:
        raise ValueError("allowlist_missing")
    return specs, active, contract, allowlist


def probe(mode, payload, ref):  # noqa: C901, PLR0912, PLR0915  # cross-check actual product provenance, binding and activation
    root = Path.cwd().resolve()
    evidence = product_trust(root, payload, ref)
    core = evidence["core"]
    # The catalog the payload was planned from, at its commit: never the floating default.
    pinned = payload.get("catalog")
    if not pinned:
        raise ValueError("catalog_unpinned: the payload names no catalog commit")
    if pinned["repository"] != KIT_REPOSITORY or not re.fullmatch(
        r"[0-9a-f]{40}", pinned["commit"]
    ):
        raise ValueError("catalog_unowned: only a full commit of the published kit is admitted")
    source = pinned["repository"]
    catalog = read_catalog(source, pinned["commit"])
    catalog_digest = digest(
        json.dumps(
            asdict(catalog), sort_keys=True, default=lambda value: value.model_dump(mode="json")
        ).encode()
    )
    if catalog_digest != payload["catalog_digest"]:
        raise ValueError("catalog_provenance_mismatch: the pinned commit holds another catalog")
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
            exported = fetch_package_source(source, component, version, workdir)
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
                    "source": source,
                }
            )
            if expected is payload["package"]:
                module, resource = payload["binding"]["resource"].split(":", 1)
                resource_path = exported / module.replace(".", "/") / resource
                content = resource_path.read_bytes()
                if digest(content) != payload["binding"]["sha256"]:
                    raise ValueError("binding_resource_changed")
                manifest = load_package_manifest(
                    exported / module.replace(".", "/") / "package.yaml"
                )
                binding = load_binding(resource_path)
                validate_binding(binding, manifest, catalog)
                if mode != "provenance":
                    # Product conflicts are the kit's check-install to classify first; these
                    # refusals guard only a release it admitted as mechanical.
                    existing = root / f"services/tg_bot/bindings/{package.name}.yaml"
                    if existing.exists() and existing.read_bytes() != content:
                        raise ValueError("binding_owned: existing product binding differs")
                    reserved = {"start", "command"}
                    for current in binding_files(root).values():
                        if current.package != package.name:
                            reserved.update(item.command for item in current.commands)
                    if reserved.intersection(item.command for item in binding.commands):
                        raise ValueError("binding_conflict: command is already owned")
                    validate_binding_settings(root, binding)
            elif component.select(_library_python_version(root)).version != expected["version"]:
                # Kit's native target-interpreter admission, never host Python.
                raise ValueError("library_incompatible: target Python requires replanning")
    evidence["component_sources"] = component_sources
    evidence["catalog"] = {
        "repository": source,
        "commit": pinned["commit"],
        "digest": catalog_digest,
    }
    if mode == "readback":
        specs, active, contract, allowlist = activation(root, package.name, selected.version)
        resource = default_binding_resource(active)
        target = root / f"services/tg_bot/bindings/{package.name}.yaml"
        if (
            target.read_bytes() != resource.read_bytes()
            or digest(target.read_bytes()) != payload["binding"]["sha256"]
        ):
            raise ValueError("installed_binding_resource_mismatch")
        validate_product_bindings(root, specs, catalog=catalog)
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


def committed_source(root, payload, git):
    """HEAD is the saved commit, its path a tracked tree, its tracked bytes unchanged.

    Returns the HEAD, the tree id and the untracked or ignored entries below the path.
    """

    def read(*args, check=True):
        return subprocess.run(
            [git, "--no-optional-locks", "--literal-pathspecs", *args],
            cwd=root,
            capture_output=True,
            text=True,
            check=check,
            timeout=60,
        )

    if Path(read("rev-parse", "--show-toplevel").stdout.strip()).resolve() != root:
        raise ValueError("source_repository_unowned: the product is not its checkout root")
    head = read("rev-parse", "--verify", "HEAD^{commit}").stdout.strip()
    if head != payload["commit"]:
        raise ValueError("source_commit_mismatch: the checkout HEAD is not the saved commit")
    spec = f"{head}:{payload['path']}"
    kind = read("cat-file", "-t", spec, check=False)
    if kind.returncode or kind.stdout.strip() != "tree":
        raise ValueError("source_path_untracked: the saved path is no tree of the saved commit")
    tree = read("rev-parse", "--verify", spec).stdout.strip()
    listed = read("ls-tree", "-r", "-z", head, "--", payload["path"]).stdout.split("\0")
    if any(entry.split(" ", 1)[0] not in {"100644", "100755"} for entry in listed if entry):
        raise ValueError("source_entry_unsupported: the source holds a link or submodule")
    status = read(
        "status",
        "--porcelain=v1",
        "-z",
        "--no-renames",
        "--untracked-files=all",
        "--ignored=matching",
        "--",
        payload["path"],
    ).stdout.split("\0")
    items = [(item[:2], item[3:].rstrip("/")) for item in status if item]
    if any(code not in {"??", "!!"} for code, _ in items):
        raise ValueError("source_changed: tracked source bytes differ from the saved commit")
    return head, tree, sorted(name for _, name in items)


def source_metadata(root, payload, core, python):
    """The source directory's static identity and package metadata, read as data only."""
    project = _product_source(root / payload["path"], root)
    package = read_source_package(project)
    expected = payload["package"]
    if (package.name, canonicalize_name(package.distribution), package.version) != (
        expected["name"],
        expected["distribution"],
        expected["version"],
    ):
        raise ValueError("package_identity_changed: the source declares another package")
    found = metadata_from_source(
        package.name, project, {"route": "package_source", "path": payload["path"]}
    )
    if found.manifest.version != package.version:
        raise ValueError("package_identity_changed: package.yaml and pyproject.toml differ")
    require_core(core, found)
    requires_python = tomllib.loads((project / "pyproject.toml").read_text())["project"].get(
        "requires-python"
    )
    if requires_python is not None and Version(python) not in SpecifierSet(requires_python):
        raise ValueError("python_incompatible: the source requires another target Python")
    binding = found.manifest.default_binding or None
    binding_sha256 = None
    if binding is not None:
        module, _, relative = binding.partition(":")
        binding_sha256 = digest((project / Path(*module.split(".")) / relative).read_bytes())
    return {
        "entry_point": package.entry_point,
        "metadata_sha256": found.provenance["metadata_sha256"],
        "requires_core": found.manifest.requires_core,
        "requires_python": requires_python,
        "default_binding": binding,
        "binding_sha256": binding_sha256,
    }


def retained_wheel(root, payload, source):
    """The wheel the backend requirement names, verified natively against the source."""
    backend = root / "services/backend"
    name, distribution, version = (
        payload["package"][key] for key in ("name", "distribution", "version")
    )
    project = tomllib.loads((backend / "pyproject.toml").read_text())
    if not any(
        canonicalize_name(Requirement(item).name) == distribution
        for item in project["project"]["dependencies"]
    ):
        raise ValueError("requirement_missing: the backend does not require the package")
    relative = project["tool"]["uv"]["sources"][distribution]["path"]
    wheel = backend / relative
    if (
        wheel.is_symlink()
        or not wheel.is_file()
        or wheel.resolve().parent != (backend / "packages").resolve()
    ):
        raise ValueError("wheel_unretained: the requirement names no retained package wheel")
    verify_source_wheel(wheel, SourcePackage(name, distribution, version, source["entry_point"]))
    built = metadata_from_wheel(name, wheel).provenance["metadata_sha256"]
    if built != source["metadata_sha256"]:
        raise ValueError("wheel_metadata_mismatch: package.yaml or default binding differs")
    wheel_sha256 = digest(wheel.read_bytes())
    lock = tomllib.loads((backend / "uv.lock").read_text())
    locked = [item for item in lock["package"] if item["name"] == distribution]
    if len(locked) != 1 or (locked[0]["version"], locked[0]["source"]) != (
        version,
        {"path": relative},
    ):
        raise ValueError("lock_mismatch: the backend lock names another artifact")
    hashes = [item["hash"] for item in locked[0]["wheels"]]
    if hashes != [f"sha256:{wheel_sha256}"]:
        raise ValueError("lock_mismatch: the backend lock names another wheel hash")
    return wheel, {
        "wheel": wheel.relative_to(root).as_posix(),
        "wheel_sha256": wheel_sha256,
        "wheel_metadata_sha256": built,
        "lock_sha256": wheel_sha256,
    }


def installed_files(site, wheel):
    """How many package members of `wheel` are installed byte-identical below `site`.

    Installer-owned `.dist-info` and relocated `.data` members are not compared.
    """
    site = site.resolve()
    with zipfile.ZipFile(wheel) as archive:
        members = [
            item
            for item in archive.infolist()
            if not item.is_dir()
            and not item.filename.split("/")[0].endswith((".dist-info", ".data"))
        ]
        for item in members:
            path = site / item.filename
            if (
                not path.resolve().is_relative_to(site)
                or path.is_symlink()
                or not path.is_file()
                or digest(path.read_bytes()) != digest(archive.read(item))
            ):
                raise ValueError(f"installed_files_mismatch: {item.filename}")
    if not members:
        raise ValueError("installed_files_mismatch: the wheel installs no package file")
    return len(members)


def installed_source(root, payload, source):
    """The retained wheel, its lock, the backend distribution and native activation."""
    name, distribution, version = (
        payload["package"][key] for key in ("name", "distribution", "version")
    )
    wheel, evidence = retained_wheel(root, payload, source)
    found = installed(root, "backend", distribution)
    if found["version"] != version:
        raise ValueError("installed_distribution_mismatch")
    if found["direct_url"] is None:
        raise ValueError("direct_url_missing: the backend records no installed artifact")
    direct = json.loads(found["direct_url"])
    recorded = direct["archive_info"].get("hashes", {}).get("sha256")
    if Path(
        url2pathname(urlparse(direct["url"]).path)
    ).resolve() != wheel.resolve() or recorded not in {None, evidence["wheel_sha256"]}:
        raise ValueError("direct_url_mismatch: the backend installed another artifact")
    site = backend_site_packages(root)
    if not site.resolve().is_relative_to((root / "services/backend/.venv").resolve()):
        raise ValueError("environment_unowned: backend site-packages")
    evidence["files_compared"] = installed_files(site, wheel)
    specs, active, _, _ = activation(root, name, version)
    evidence["binding_sha256"] = None
    if source["binding_sha256"] is not None:
        target = root / f"services/tg_bot/bindings/{name}.yaml"
        resource = default_binding_resource(active)
        if (
            target.read_bytes() != resource.read_bytes()
            or digest(target.read_bytes()) != source["binding_sha256"]
        ):
            raise ValueError("installed_binding_resource_mismatch")
        validate_product_bindings(root, specs)
        evidence["binding_sha256"] = source["binding_sha256"]
    return evidence | {
        "direct_url": direct["url"],
        "version": found["version"],
        "manifest_sha256": active.manifest_sha256,
    }


def source_probe(mode, payload, ref):
    """One `LocalPackageInstall` read without building, installing, fetching or importing it."""
    root = Path.cwd().resolve()
    if payload["route"] != "package_source":
        raise ValueError("route_mismatch: source modes read a package_source target")
    trusted = product_trust(root, payload, ref)
    git = shutil.which("git")
    if git is None:
        raise ValueError("git_missing: product installation requires git")
    head, tree, residue = committed_source(root, payload, git)
    if SOURCE_MODES[mode] == "provenance" and residue:
        raise ValueError("source_uncommitted: the source directory holds uncommitted entries")
    python = _library_python_version(root)
    source = {"tree": tree, **source_metadata(root, payload, trusted["core"], python)}
    return {
        "mode": SOURCE_MODES[mode],
        "target": payload,
        "head": head,
        "core_version": trusted["core"],
        "python_version": python,
        "tooling_commit": trusted["tooling"]["vcs_info"]["commit_id"],
        "source": source,
        "residue": residue,
        "installed": (
            installed_source(root, payload, source) if mode == "source-readback" else None
        ),
    }


if __name__ == "__main__":
    mode, payload, ref = sys.argv[1], json.loads(sys.argv[2]), sys.argv[3]
    reader = source_probe if mode in SOURCE_MODES else probe
    sys.stdout.write(json.dumps(reader(mode, payload, ref)) + "\n")
