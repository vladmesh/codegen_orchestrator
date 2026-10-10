"""The stand's witness of one native catalog install, against the installer it observes.

`check_execution` holds the commands the scaffolder's `run_install` recorded for a published
operation to the installer's own fixed sequence: the private attempt checkout of the admitted
base prepared by the product's prepare-env, the kit's read-only check-install answered with its
typed result, the closure's components, binding, generation, validation, readback, commit and
push. A nonzero command is accepted only at that check-install, and only with the exit its typed
status maps to (`PREFLIGHT_EXIT_CODES`), its target, catalog and core matching the admitted
closure, and no glue the closure does not perform itself (`InstallPreflight.outstanding_glue`).

The final stand (`mechanical_install`) and the free runner proof (`tests/runner`) both hand
their actual executor results to this module, so the stand's verdict is proven on a real trace.
It imports only shared contracts: both callers run it outside the scaffolder.
"""

import json

from pydantic import ValidationError

from shared.contracts.dto.catalog_install import (
    PREFLIGHT_EXIT_CODES,
    CatalogInstall,
    InstallPreflight,
)
from shared.workspace_preservation import CATALOG_INSTALL_ATTEMPTS

#: The scaffolder's log event that carries a published operation's executed stages.
PUBLISHED_EVENT = "catalog_install_published"
#: The installer's stages in the order it runs them; `library` only for a closure with libraries.
STAGES = (
    "prepare",
    "preflight",
    "package",
    "library",
    "bind",
    "generate",
    "validate",
    "readback",
    "commit",
    "push",
)
GIT = ["git", "-c", "core.hooksPath=/dev/null"]
#: The environments the installer prepares and checks, as `scripts/prepare-env.sh` names them.
PRODUCT_ENVIRONMENTS = ("root", "backend", "tg_bot")
#: The unreachable Redis the installer's unit leg runs against.
UNIT_LEG_REDIS_URL = "redis://redis.invalid:6379"
#: Diagnostics stay bounded: an observed argv keeps this many arguments of this many characters.
MAX_ARGS, MAX_ARG = 16, 200


class _Payload:
    """The install probe's payload argument: the admitted closure as JSON."""


PAYLOAD = _Payload()


class WitnessRefused(ValueError):
    """The observed install is not the installer's trustworthy trace, and the observed row why."""

    def __init__(self, reason: str, index: int | None = None, row: object = None) -> None:
        super().__init__(reason)
        self.reason, self.index = reason, index
        self.row = row if isinstance(row, dict) else {}

    def disposition(self) -> dict:
        argv = self.row.get("argv")
        return {
            "accepted": False,
            "reason": self.reason[:1000],
            "index": self.index,
            "stage": self.row.get("stage"),
            "returncode": self.row.get("returncode"),
            "argv": [str(arg)[:MAX_ARG] for arg in argv[:MAX_ARGS]]
            if isinstance(argv, list)
            else None,
        }


def _well_formed(row: object) -> bool:
    return (
        isinstance(row, dict)
        and set(row) == {"stage", "argv", "returncode"}
        and isinstance(row["stage"], str)
        and isinstance(row["argv"], list)
        and bool(row["argv"])
        and all(isinstance(arg, str) for arg in row["argv"])
        and type(row["returncode"]) is int
    )


def _groups(stages: list[dict]) -> list[str]:
    groups: list[str] = []
    for row in stages:
        if not groups or groups[-1] != row["stage"]:
            groups.append(row["stage"])
    return groups


def _matches(expected: object, actual: str, payload: dict) -> bool:
    if expected is PAYLOAD:
        try:
            return json.loads(actual) == payload
        except ValueError:
            return False
    if isinstance(expected, frozenset):
        return actual in expected
    return expected == actual


def _expected(  # noqa: PLR0913 - every identity the installer's commands name
    install: CatalogInstall,
    *,
    root: str,
    probe: str,
    ref: str,
    operation_id: str,
    story_id: str,
    base_sha: str,
) -> list[tuple[str, list]]:
    """The commands `run_install` records for a published operation, in order."""
    branch = f"story/{story_id}"
    heads = [*GIT, "ls-remote", "--heads", "origin", f"refs/heads/{branch}"]
    status = [*GIT, "status", "--porcelain", "--untracked-files=all"]
    kit, python = f"{root}/.venv/bin/kit", f"{root}/.venv/bin/python"
    package = install.package.name
    catalog = ["--catalog-source", install.catalog.repository]
    catalog += ["--catalog-ref", install.catalog.commit]
    # The base is the owned remote story head when it exists, else the scaffold's main.
    start = frozenset({"refs/remotes/origin/main^{commit}", f"{base_sha}^{{commit}}"})
    return [
        ("prepare", [*GIT, "remote", "get-url", "origin"]),
        ("prepare", [*GIT, "check-ref-format", "--branch", branch]),
        ("prepare", [*GIT, "fetch", "--no-tags", "origin"]),
        ("prepare", heads),
        ("prepare", [*GIT, "rev-parse", "--verify", start]),
        ("prepare", [*GIT, "worktree", "add", "--detach", root, base_sha]),
        ("prepare", [*GIT, "ls-files"]),
        ("prepare", ["sh", "scripts/prepare-env.sh", *PRODUCT_ENVIRONMENTS]),
        ("preflight", status),
        ("preflight", [python, "-I", probe, "provenance", PAYLOAD, ref]),
        (
            "preflight",
            [
                kit,
                "check-install",
                package,
                "--json",
                *catalog,
                "--version",
                install.package.version,
                "--product-root",
                root,
            ],
        ),
        ("preflight", status),
        ("preflight", [python, "-I", probe, "preflight", PAYLOAD, ref]),
        ("preflight", status),
        ("package", [kit, "add", package, *catalog]),
        *(("library", [kit, "add", item.name, *catalog]) for item in install.libraries),
        ("bind", [kit, "bind", package, "--default"]),
        ("generate", ["make", "generate-from-spec"]),
        ("validate", ["make", "validate-specs"]),
        *(
            ("validate", [f"{root}/services/{service}/.venv/bin/mypy", f"services/{service}"])
            for service in ("backend", "tg_bot")
        ),
        ("validate", ["make", "tests", f"REDIS_URL={UNIT_LEG_REDIS_URL}"]),
        ("readback", [python, "-I", probe, "readback", PAYLOAD, ref]),
        ("commit", [*GIT, "add", "-A"]),
        ("commit", [*GIT, "status", "--porcelain"]),
        (
            "commit",
            [
                *GIT,
                "-c",
                "user.name=Codegen Bot",
                "-c",
                "user.email=codegen@localhost",
                "commit",
                "-m",
                f"Install catalog package {package} ({operation_id})",
            ],
        ),
        ("commit", [*GIT, "rev-parse", "HEAD"]),
        ("push", heads),
        ("push", [*GIT, "push", "origin", f"HEAD:refs/heads/{branch}"]),
        ("push", heads),
    ]


#: Where the check-install command sits in `_expected`: the one command that may exit nonzero.
CHECK_INSTALL_INDEX = 10


def _typed(preflight: object, install: object) -> tuple[InstallPreflight, CatalogInstall]:
    try:
        closure = CatalogInstall.model_validate(install)
    except ValidationError as error:
        raise WitnessRefused(
            f"admitted closure is malformed: {error.error_count()} errors"
        ) from error
    if closure.catalog is None:
        raise WitnessRefused("admitted closure names no catalog commit")
    if preflight is None:
        raise WitnessRefused("the operation retained no typed preflight result")
    try:
        result = InstallPreflight.model_validate(preflight)
    except ValidationError as error:
        errors = "; ".join(
            f"{'.'.join(str(part) for part in item['loc'])}: {item['type']}"
            for item in error.errors()[:5]
        )
        raise WitnessRefused(f"typed preflight result is malformed: {errors}") from error
    return result, closure


def check_execution(  # noqa: C901, PLR0912, PLR0913 - one ordered witness over every identity
    stages: object,
    *,
    preflight: object,
    install: object,
    operation_id: str,
    story_id: str,
    checkout: str,
    base_sha: str,
) -> dict:
    """The accepted disposition of this published operation's trace, or `WitnessRefused`."""
    if not isinstance(stages, list) or not stages:
        raise WitnessRefused("the native executor retained no stages")
    for index, row in enumerate(stages):
        if not _well_formed(row):
            raise WitnessRefused("malformed native stage row", index, row)
    for index, row in enumerate(stages):
        argv = row["argv"]
        if argv[0] == "git" and argv[1:3] != GIT[1:]:
            raise WitnessRefused("native Git did not bypass product hooks", index, row)
        if argv[0] == "git" and any(arg.startswith("--force") or arg == "-f" for arg in argv):
            raise WitnessRefused("native Git unexpectedly forced publication", index, row)
    result, closure = _typed(preflight, install)
    required = [stage for stage in STAGES if stage != "library" or closure.libraries]
    observed = _groups(stages)
    if observed != required:
        raise WitnessRefused(
            "native executor did not retain every required stage in order: "
            f"expected {required}, observed {observed}"
        )
    # The isolated attempt checkout: derived from this operation, detached at its admitted base.
    worktree = next(
        (
            (index, row)
            for index, row in enumerate(stages)
            if row["stage"] == "prepare" and row["argv"][3:6] == ["worktree", "add", "--detach"]
        ),
        None,
    )
    if worktree is None or len(worktree[1]["argv"]) != 8:
        raise WitnessRefused("preparation created no detached attempt checkout")
    index, row = worktree
    root = row["argv"][6]
    if not (
        checkout.endswith(f"/{operation_id}")
        and root.endswith(f"/{CATALOG_INSTALL_ATTEMPTS}/{checkout}")
    ):
        raise WitnessRefused(
            f"the attempt checkout is not operation-owned: {checkout} for {operation_id}",
            index,
            row,
        )
    if row["argv"][7] != base_sha:
        raise WitnessRefused("the attempt checkout is not the admitted base", index, row)
    first_probe = stages[CHECK_INSTALL_INDEX - 1] if len(stages) > CHECK_INSTALL_INDEX else None
    provenance = first_probe["argv"] if first_probe else []
    probe = provenance[2] if len(provenance) == 6 else ""
    ref = provenance[5] if len(provenance) == 6 else ""
    if not probe.endswith("/install_probe.py"):
        raise WitnessRefused(
            "preflight did not start with the fixed install probe",
            CHECK_INSTALL_INDEX - 1,
            first_probe,
        )
    expected = _expected(
        closure,
        root=root,
        probe=probe,
        ref=ref,
        operation_id=operation_id,
        story_id=story_id,
        base_sha=base_sha,
    )
    payload = json.loads(closure.model_dump_json())
    for index, ((stage, argv), row) in enumerate(zip(expected, stages, strict=False)):
        if row["stage"] != stage or not (
            len(argv) == len(row["argv"])
            and all(
                _matches(want, got, payload) for want, got in zip(argv, row["argv"], strict=True)
            )
        ):
            raise WitnessRefused(
                f"native command {index} differs from the installer's {stage} command",
                index,
                row,
            )
    if len(stages) != len(expected):
        index = min(len(stages), len(expected))
        raise WitnessRefused(
            f"native command sequence differs from the installer's: {len(stages)} commands, "
            f"expected {len(expected)}",
            index,
            stages[index] if index < len(stages) else None,
        )
    for index, row in enumerate(stages):
        if index != CHECK_INSTALL_INDEX and row["returncode"] != 0:
            raise WitnessRefused("native stage failed", index, row)
    typed_row = stages[CHECK_INSTALL_INDEX]
    if typed_row["returncode"] != PREFLIGHT_EXIT_CODES[result.status]:
        raise WitnessRefused(
            f"preflight_exit_mismatch: {result.status} exited {typed_row['returncode']}",
            CHECK_INSTALL_INDEX,
            typed_row,
        )
    if result.status == "incompatible":
        raise WitnessRefused(
            f"preflight_incompatible: {result.incompatible.code}", CHECK_INSTALL_INDEX, typed_row
        )
    if (mismatch := result.provenance_mismatch(closure)) is not None:
        raise WitnessRefused(
            f"preflight_provenance_mismatch: {mismatch}", CHECK_INSTALL_INDEX, typed_row
        )
    if outstanding := result.outstanding_glue(closure):
        items = ", ".join(f"{item.code} by {item.owner} ({item.symbol})" for item in outstanding)
        raise WitnessRefused(
            f"preflight glue outstanding beyond the admitted closure: {items}"[:1000],
            CHECK_INSTALL_INDEX,
            typed_row,
        )
    return {
        "accepted": True,
        "stages": observed,
        "commands": len(stages),
        "checkout": checkout,
        "base_sha": base_sha,
        "libraries": [item.name for item in closure.libraries],
        "preflight": {
            "status": result.status,
            "returncode": typed_row["returncode"],
            "resolved_by_closure": [item.symbol for item in result.glue],
        },
    }


def witness_publication(artifact: dict, rows: list[dict], operation: dict, install: dict) -> list:
    """Retain the operation's one publication in `artifact["execution"]`, then witness it.

    Everything the verdict reads is written down first, so a refusal still leaves the actual
    stages, the operation's base, head, checkout and typed preflight, the admitted closure and
    the refusal's observed stage, index and return code. The lease token is never copied.
    """
    matches = [
        row
        for row in rows
        if row.get("event") == PUBLISHED_EVENT and row.get("operation_id") == operation["id"]
    ]
    retained = artifact["execution"] = {
        "operation_id": operation["id"],
        "observations": len(matches),
        "base_sha": operation.get("base_sha"),
        "head_sha": operation.get("head_sha"),
        "checkout": operation.get("checkout"),
        "preflight": operation.get("preflight"),
        "closure": install,
    }
    if len(matches) == 1:
        retained["published_head_sha"] = matches[0].get("head_sha")
        retained["stages"] = matches[0].get("execution_stages")
    try:
        if len(matches) != 1:
            raise WitnessRefused(
                f"expected one native publication observation, found {len(matches)}"
            )
        if retained["published_head_sha"] != operation["head_sha"]:
            raise WitnessRefused("published head differs from the operation's head")
        retained["witness"] = check_execution(
            retained["stages"],
            preflight=operation.get("preflight"),
            install=install,
            operation_id=operation["id"],
            story_id=operation["story_id"],
            checkout=operation["checkout"],
            base_sha=operation["base_sha"],
        )
    except WitnessRefused as refusal:
        retained["witness"] = refusal.disposition()
        raise
    return retained["stages"]
