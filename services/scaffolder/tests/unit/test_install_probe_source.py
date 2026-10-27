"""The source probe reads a package source and its retained wheel as data, through the kit."""

import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import zipfile

from framework.package_source import PackageSourceError, PackageWheelMismatchError
from framework.preflight import PreflightIncompatibleError
import pytest

from src.install import owned_tree
from src.install_probe import (
    installed_files,
    library_python_version,
    retained_wheel,
    source_metadata,
)

DATA = Path(__file__).resolve().parents[3] / "langgraph/tests/unit/fixtures/catalog-install-v2"
SOURCE = "packages/notebook"
MODULE = "codegen_kit_notebook"
DISTRIBUTION = "codegen-kit-notebook"
WHEEL = "codegen_kit_notebook-0.1.0-py3-none-any.whl"
CORE = "2.6.0"
PYTHON = "3.12.11"
PAYLOAD = {
    "route": "package_source",
    "path": SOURCE,
    "package": {"name": "notebook", "distribution": DISTRIBUTION, "version": "0.1.0"},
}
PYPROJECT = (
    '[project]\nname = "codegen-kit-notebook"\nversion = "0.1.0"\nrequires-python = ">=3.12"\n'
    '[project.entry-points."codegen_kit.packages"]\nnotebook = "codegen_kit_notebook:package"\n'
)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def product(tmp_path, pyproject=PYPROJECT) -> Path:
    root = tmp_path / "product"
    module = root / SOURCE / MODULE
    (module / "bindings").mkdir(parents=True)
    shutil.copy(DATA / "package.yaml", module / "package.yaml")
    shutil.copy(DATA / "default.yaml", module / "bindings/default.yaml")
    (module / "__init__.py").write_text("raise RuntimeError('package runtime imported')\n")
    (root / SOURCE / "pyproject.toml").write_text(pyproject)
    (root / ".copier-answers.yml").write_text("_commit: synthetic-fixture\n")
    return root


def payload(**package):
    return PAYLOAD | {"package": PAYLOAD["package"] | package}


def test_a_source_reports_its_static_identity_metadata_and_default_binding(tmp_path):
    root = product(tmp_path)
    found = source_metadata(root, PAYLOAD, CORE, PYTHON)
    binding = (DATA / "default.yaml").read_bytes()
    assert found["entry_point"] == "codegen_kit_notebook:package"
    assert (found["default_binding"], found["binding_sha256"]) == (
        "codegen_kit_notebook:bindings/default.yaml",
        sha256(binding),
    )
    assert (found["requires_core"], found["requires_python"]) == (">=2.2,<3", ">=3.12")
    assert len(found["metadata_sha256"]) == 64


def test_a_source_without_a_default_binding_reports_no_binding_digest(tmp_path):
    root = product(tmp_path)
    manifest = root / SOURCE / MODULE / "package.yaml"
    manifest.write_text(
        manifest.read_text().replace(
            "default_binding: codegen_kit_notebook:bindings/default.yaml\n", ""
        )
    )
    found = source_metadata(root, PAYLOAD, CORE, PYTHON)
    assert (found["default_binding"], found["binding_sha256"]) == (None, None)


def nested(root):
    (root / "packages/.copier-answers.yml").write_text("_commit: synthetic-fixture\n")


def escaping(root):
    outside = root.parent / "elsewhere"
    shutil.move(root / SOURCE, outside)
    (root / SOURCE).symlink_to(outside, target_is_directory=True)


def binding_outside_module(root):
    binding = root / SOURCE / MODULE / "bindings/default.yaml"
    shutil.move(binding, root / "default.yaml")
    binding.symlink_to(root / "default.yaml")


def manifest_version(root):
    manifest = root / SOURCE / MODULE / "package.yaml"
    manifest.write_text(manifest.read_text().replace("version: 0.1.0", "version: 0.1.1", 1))


@pytest.mark.parametrize(
    ("change", "error", "match"),
    [
        (nested, PackageSourceError, "belongs to another product"),
        (escaping, PackageSourceError, "is not inside product"),
        (binding_outside_module, PreflightIncompatibleError, "missing from the source"),
        (manifest_version, ValueError, "package_identity_changed: package.yaml"),
    ],
    ids=["nested-product", "symlink-outside", "binding-escapes-module", "manifest-version"],
)
def test_a_source_the_kit_would_refuse_is_refused_as_data(tmp_path, change, error, match):
    root = product(tmp_path)
    change(root)
    with pytest.raises(error, match=match):
        source_metadata(root, PAYLOAD, CORE, PYTHON)


@pytest.mark.parametrize(
    "package",
    [{"name": "notes"}, {"distribution": "codegen-kit-notes"}, {"version": "0.1.1"}],
)
def test_a_source_declaring_another_package_than_saved_is_refused(tmp_path, package):
    with pytest.raises(ValueError, match="package_identity_changed: the source declares"):
        source_metadata(product(tmp_path), payload(**package), CORE, PYTHON)


def test_a_source_the_product_core_cannot_run_is_refused(tmp_path):
    with pytest.raises(PreflightIncompatibleError, match="core_range"):
        source_metadata(product(tmp_path), PAYLOAD, "3.0.0", PYTHON)


def test_a_source_the_target_python_cannot_run_is_refused(tmp_path):
    with pytest.raises(ValueError, match="python_incompatible"):
        source_metadata(product(tmp_path), PAYLOAD, CORE, "3.11.9")


def virtualenv_interpreter(args, **kwargs):
    """A product venv's CPython: its `.pth` startup import caches bytecode unless `-B`.

    `-I` ignores `PYTHONDONTWRITEBYTECODE`, so only the flag keeps the environment unchanged.
    """
    environment = Path(args[0]).parents[1]
    if "-B" not in args[1 : args.index("-c")]:
        cache = environment / "lib/python3.12/site-packages/__pycache__"
        cache.mkdir(exist_ok=True)
        (cache / "_virtualenv.cpython-312.pyc").write_bytes(b"bytecode")
    stdout = json.dumps({"prefix": str(environment), "version": PYTHON})
    return subprocess.CompletedProcess(args, 0, stdout=stdout + "\n", stderr="")


def test_the_library_python_version_read_leaves_the_environment_byte_identical(
    tmp_path, monkeypatch
):
    environment = tmp_path / "services/tg_bot/.venv"
    (environment / "lib/python3.12/site-packages").mkdir(parents=True)
    (environment / "lib/python3.12/site-packages/_virtualenv.pth").write_text(
        "import _virtualenv\n"
    )
    (environment / "bin").mkdir()
    (environment / "bin/python").write_text("")
    monkeypatch.setattr("src.install_probe.subprocess.run", virtualenv_interpreter)
    before = owned_tree(environment)
    assert library_python_version(tmp_path) == PYTHON
    assert owned_tree(environment) == before


def build_wheel(root, edit=None) -> Path:
    """The members `kit add --package-source` builds, written as a fixture archive."""
    source = root / SOURCE
    members = {
        f"{MODULE}/__init__.py": (source / MODULE / "__init__.py").read_bytes(),
        f"{MODULE}/package.yaml": (source / MODULE / "package.yaml").read_bytes(),
        f"{MODULE}/bindings/default.yaml": (source / MODULE / "bindings/default.yaml").read_bytes(),
        f"{MODULE}-0.1.0.dist-info/METADATA": b"Name: codegen-kit-notebook\nVersion: 0.1.0\n",
        f"{MODULE}-0.1.0.dist-info/entry_points.txt": (
            b"[codegen_kit.packages]\nnotebook = codegen_kit_notebook:package\n"
        ),
    }
    if edit is not None:
        edit(members)
    wheel = root / "services/backend/packages" / WHEEL
    wheel.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(wheel, "w") as archive:
        for name, content in members.items():
            archive.writestr(name, content)
    return wheel


def retain(root, wheel, lock_hash=None, requirement=DISTRIBUTION):
    backend = root / "services/backend"
    (backend / "pyproject.toml").write_text(
        f'[project]\nname = "backend"\ndependencies = ["fastapi", "{requirement}"]\n'
        f'[tool.uv.sources]\n{DISTRIBUTION} = {{ path = "packages/{wheel.name}" }}\n'
    )
    (backend / "uv.lock").write_text(
        'version = 1\n[[package]]\nname = "backend"\nversion = "0.1.0"\n'
        f'[[package]]\nname = "{DISTRIBUTION}"\nversion = "0.1.0"\n'
        f'source = {{ path = "packages/{wheel.name}" }}\n'
        f'wheels = [{{ filename = "{wheel.name}", '
        f'hash = "sha256:{lock_hash or sha256(wheel.read_bytes())}" }}]\n'
    )


def test_the_retained_wheel_is_the_sources_package_in_requirement_and_lock(tmp_path):
    root = product(tmp_path)
    source = source_metadata(root, PAYLOAD, CORE, PYTHON)
    wheel = build_wheel(root)
    retain(root, wheel)
    found, evidence = retained_wheel(root, PAYLOAD, source)
    assert found == wheel
    assert evidence == {
        "wheel": f"services/backend/packages/{WHEEL}",
        "wheel_sha256": sha256(wheel.read_bytes()),
        "wheel_metadata_sha256": source["metadata_sha256"],
        "lock_sha256": sha256(wheel.read_bytes()),
    }


def other_binding(members):
    members[f"{MODULE}/bindings/default.yaml"] += b"# product edit\n"


def other_entry_point(members):
    members[f"{MODULE}-0.1.0.dist-info/entry_points.txt"] = (
        b"[codegen_kit.packages]\nnotebook = codegen_kit_notebook:other\n"
    )


@pytest.mark.parametrize(
    ("edit", "error", "match"),
    [
        (other_binding, ValueError, "wheel_metadata_mismatch"),
        (other_entry_point, PackageWheelMismatchError, "entry points"),
    ],
)
def test_a_wheel_that_is_not_the_sources_build_is_refused(tmp_path, edit, error, match):
    root = product(tmp_path)
    source = source_metadata(root, PAYLOAD, CORE, PYTHON)
    retain(root, build_wheel(root, edit))
    with pytest.raises(error, match=match):
        retained_wheel(root, PAYLOAD, source)


def test_a_lock_naming_another_wheel_hash_is_refused(tmp_path):
    root = product(tmp_path)
    source = source_metadata(root, PAYLOAD, CORE, PYTHON)
    retain(root, build_wheel(root), lock_hash="0" * 64)
    with pytest.raises(ValueError, match="lock_mismatch: the backend lock names another wheel"):
        retained_wheel(root, PAYLOAD, source)


def test_a_backend_without_the_package_requirement_is_refused(tmp_path):
    root = product(tmp_path)
    source = source_metadata(root, PAYLOAD, CORE, PYTHON)
    retain(root, build_wheel(root), requirement="codegen-kit-other")
    with pytest.raises(ValueError, match="requirement_missing"):
        retained_wheel(root, PAYLOAD, source)


def test_a_wheel_linked_from_outside_the_backend_packages_is_refused(tmp_path):
    root = product(tmp_path)
    source = source_metadata(root, PAYLOAD, CORE, PYTHON)
    wheel = build_wheel(root)
    elsewhere = tmp_path / WHEEL
    shutil.move(wheel, elsewhere)
    wheel.symlink_to(elsewhere)
    retain(root, elsewhere)
    with pytest.raises(ValueError, match="wheel_unretained"):
        retained_wheel(root, PAYLOAD, source)


def site_packages(tmp_path, wheel) -> Path:
    site = tmp_path / "site-packages"
    with zipfile.ZipFile(wheel) as archive:
        archive.extractall(site)
    return site


def test_installed_package_files_are_the_retained_wheels_bytes(tmp_path):
    root = product(tmp_path)
    wheel = build_wheel(root)
    site = site_packages(tmp_path, wheel)
    (site / f"{MODULE}-0.1.0.dist-info/RECORD").write_text("installer-owned\n")
    assert installed_files(site, wheel) == 3


@pytest.mark.parametrize("change", ["edit", "remove"])
def test_an_installed_file_differing_from_the_retained_wheel_is_refused(tmp_path, change):
    root = product(tmp_path)
    wheel = build_wheel(root)
    site = site_packages(tmp_path, wheel)
    installed = site / MODULE / "__init__.py"
    if change == "edit":
        installed.write_text("package = 'substituted'\n")
    else:
        installed.unlink()
    with pytest.raises(ValueError, match="installed_files_mismatch: codegen_kit_notebook/__init__"):
        installed_files(site, wheel)
