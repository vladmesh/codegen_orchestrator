"""CI-only genuine release render, exported with the producer's tracked bytes."""

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile

from scripts.template_pin import TEMPLATE_PIN


def render(output: Path):
    output.mkdir(parents=True, exist_ok=True)
    git = shutil.which("git")
    copier = shutil.which("copier")
    if git is None or copier is None:
        raise RuntimeError("git and copier are required to render the released fixture")
    with tempfile.TemporaryDirectory(prefix="kit-fixture-") as scratch:
        root = Path(scratch)
        product = root / "product"
        subprocess.run(
            [
                copier,
                "copy",
                "--trust",
                "--defaults",
                f"--vcs-ref={TEMPLATE_PIN.ref}",
                "--data",
                "project_name=env_fixture",
                "--data",
                "modules=backend,tg_bot",
                TEMPLATE_PIN.source,
                str(product),
            ],
            check=True,
            timeout=600,
        )
        subprocess.run([git, "init", "-q", str(product)], check=True)
        subprocess.run([git, "add", "-A"], cwd=product, check=True)
        tracked = subprocess.check_output([git, "ls-files", "-z"], cwd=product).decode().split("\0")
        hashes = {}
        with tarfile.open(output / "fixture.tar.gz", "w:gz") as archive:
            for name in filter(None, tracked):
                hashes[name] = hashlib.sha256((product / name).read_bytes()).hexdigest()
                archive.add(product / name, arcname=f"{TEMPLATE_PIN.fixture_dirname}/{name}")
        (output / "provenance.json").write_text(
            json.dumps(
                {
                    "source": TEMPLATE_PIN.source,
                    "ref": TEMPLATE_PIN.ref,
                    "candidate_sha": subprocess.check_output([git, "rev-parse", "HEAD"])
                    .decode()
                    .strip(),
                    "answers": (product / ".copier-answers.yml").read_text(),
                    "tracked_sha256": hashes,
                },
                indent=2,
            )
            + "\n"
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, required=True)
    render(parser.parse_args().output_dir)
