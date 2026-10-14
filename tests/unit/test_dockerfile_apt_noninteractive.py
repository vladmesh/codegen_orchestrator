"""Every Dockerfile stage that runs `apt-get` declares `ARG DEBIAN_FRONTEND=noninteractive`.

Without it debconf finds no terminal and prints its fallback cascade (Dialog, Readline,
Teletype) into every build log. It is an `ARG`, never an `ENV`: an `ENV` would leak into
the running container. An `ARG` is scoped to its stage, so each stage that runs
`apt-get` declares it before its first `apt-get`.

The vendored codegen-product-kit snapshot is another repository's tree and is not scanned.
"""

import os
from pathlib import Path
import re

import pytest

REPO_ROOT = Path(__file__).parents[2]
FIXTURES = REPO_ROOT / "shared/tests/fixtures"
ARG_LINE = "ARG DEBIAN_FRONTEND=noninteractive"


def _dockerfiles() -> list[Path]:
    found = []
    for directory, subdirs, files in os.walk(REPO_ROOT):
        here = Path(directory)
        subdirs[:] = [
            name
            for name in subdirs
            if not name.startswith(".")
            and name not in ("node_modules", "__pycache__")
            and not (here == FIXTURES and name.startswith("codegen-product-kit-"))
        ]
        found += [here / name for name in files if name.startswith("Dockerfile")]
    return sorted(found)


def _instructions(text: str) -> list[str]:
    """Logical instructions: continuations joined, comment lines dropped."""
    instructions, current = [], ""
    for line in text.splitlines():
        if line.lstrip().startswith("#"):
            continue
        current += line.rstrip().removesuffix("\\")
        if not line.rstrip().endswith("\\"):
            if current.strip():
                instructions.append(current.strip())
            current = ""
    if current.strip():
        instructions.append(current.strip())
    return instructions


def violations(text: str) -> list[str]:
    found = []
    declared = False
    for instruction in _instructions(text):
        keyword, _, rest = instruction.partition(" ")
        keyword = keyword.upper()
        if keyword == "FROM":
            declared = False
        elif keyword == "ARG" and rest.strip() == ARG_LINE.removeprefix("ARG "):
            declared = True
        elif keyword == "ENV" and re.search(r"\bDEBIAN_FRONTEND\b", rest):
            found.append(f"sets DEBIAN_FRONTEND through ENV: {instruction}")
        elif keyword == "RUN" and re.search(r"\bapt-get\b", rest) and not declared:
            found.append(f"runs apt-get before {ARG_LINE} in its stage: {instruction[:80]}")
    return found


DOCKERFILES = _dockerfiles()


def test_the_scan_finds_the_apt_get_dockerfiles():
    assert REPO_ROOT / "services/api/Dockerfile" in DOCKERFILES
    assert not any(FIXTURES in path.parents for path in DOCKERFILES)


@pytest.mark.parametrize("dockerfile", DOCKERFILES, ids=lambda p: str(p.relative_to(REPO_ROOT)))
def test_apt_get_stages_are_noninteractive(dockerfile: Path):
    assert violations(dockerfile.read_text()) == []


def test_each_stage_needs_its_own_arg():
    text = (
        f"FROM python:3.12-slim AS builder\n{ARG_LINE}\nRUN apt-get update\n"
        "FROM python:3.12-slim\nRUN apt-get update \\\n    && apt-get install -y git\n"
    )

    assert len(violations(text)) == 1


def test_an_arg_before_the_first_from_does_not_count():
    assert len(violations(f"{ARG_LINE}\nFROM python:3.12-slim\nRUN apt-get update\n")) == 1


def test_an_env_is_refused():
    text = f"FROM python:3.12-slim\n{ARG_LINE}\nENV DEBIAN_FRONTEND=noninteractive\n"

    assert len(violations(text)) == 1
