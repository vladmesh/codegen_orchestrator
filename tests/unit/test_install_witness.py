"""The stand witness accepts the current native install trace and refuses every unsafe variant.

The trace below has the shape the scaffolder's `run_install` produced in the free runner proof
of main c43f0c38 (reminders on a product without textparse: prepare, then kit check-install exit
3 with the closure's own `library_required`). These are pure inputs; the real producer/consumer
junction is the runner proof, which hands its actual results to the same witness.
"""

import copy
import json

import pytest

from scripts.template_pin import TEMPLATE_PIN
from tests.live.install_witness import (
    PUBLISHED_EVENT,
    WitnessRefused,
    check_execution,
    witness_publication,
)
from tests.runner.verify_evidence import stand_witness_problems

G = ["git", "-c", "core.hooksPath=/dev/null"]
CATALOG = "https://github.com/vladmesh/codegen-product-kit.git"
# The admitted catalog and kit tooling are the same immutable commit the template pins.
KIT_COMMIT = TEMPLATE_PIN.ref
BASE = "c1cf3226e0edf07a5dc9e4e7b1a7f227c74e46ca"
HEAD = "2e5e5f674ddfa52402338db14ceacbe8110334f3"
OPERATION = "install-712031010ab1489b9371e9664ff06478"
CHECKOUT = f"repo-e66cd313/{OPERATION}"
ROOT = f"/srv/workspaces/.catalog-install-attempts/{CHECKOUT}"
PROBE = "/app/src/install_probe.py"
STORY = "story-52d85de8"


def component(name, version):
    return {
        "name": name,
        "distribution": f"codegen-kit-{name}",
        "version": version,
        "tag": f"packages/{name}/v{version}",
    }


def closure(package="reminders", libraries=("textparse",)):
    return {
        "package": component(package, "0.5.0"),
        "libraries": [component(name, "0.1.0") for name in libraries],
        "binding": {
            "package": package,
            "resource": f"codegen_kit_{package.replace('-', '_')}:bindings/default.yaml",
            "sha256": "c" * 64,
            "functions": [f"{name}.when" for name in libraries],
        },
        "core_version": "2.5.0",
        "python_version": "3.12.0",
        "catalog_digest": "d" * 64,
        "tooling_commit": KIT_COMMIT,
        "catalog": {"repository": CATALOG, "commit": KIT_COMMIT, "catalog_sha256": "a" * 64},
    }


def library_glue(symbol="textparse", owner="package:reminders", code="library_required"):
    return {
        "code": code,
        "path": "services/tg_bot/pyproject.toml",
        "line": None,
        "owner": owner,
        "symbol": symbol,
        "key": None,
        "command": "remind",
        "conflict": f"/remind parses with library '{symbol}'",
        "action": f"run `kit add {symbol}` before installing reminders",
        "other": None,
    }


def preflight(package="reminders", glue=None):
    glue = [library_glue()] if glue is None else glue
    return {
        "result_version": 1,
        "package": package,
        "status": "glue" if glue else "mechanical",
        "product_core": "2.5.0",
        "target": {
            "route": "catalog",
            "catalog_source": CATALOG,
            "catalog_ref": KIT_COMMIT,
            "tag": f"packages/{package}/v0.5.0",
            "version": "0.5.0",
            "requires_core": ">=2.2,<3",
            "metadata_sha256": "e" * 64,
        },
        "glue": glue,
        "incompatible": None,
    }


def trace(install, *, check_rc=3, remote_story=False):
    """The commands `run_install` records, in order, for this closure."""
    payload = json.dumps(install, separators=(",", ":"))
    package = install["package"]["name"]
    branch = f"story/{STORY}"
    heads = [*G, "ls-remote", "--heads", "origin", f"refs/heads/{branch}"]
    status = [*G, "status", "--porcelain", "--untracked-files=all"]
    kit, python = f"{ROOT}/.venv/bin/kit", f"{ROOT}/.venv/bin/python"
    catalog = ["--catalog-source", CATALOG, "--catalog-ref", KIT_COMMIT]
    start = BASE if remote_story else "refs/remotes/origin/main"
    rows = [
        ("prepare", [*G, "remote", "get-url", "origin"]),
        ("prepare", [*G, "check-ref-format", "--branch", branch]),
        ("prepare", [*G, "fetch", "--no-tags", "origin"]),
        ("prepare", heads),
        ("prepare", [*G, "rev-parse", "--verify", f"{start}^{{commit}}"]),
        ("prepare", [*G, "worktree", "add", "--detach", ROOT, BASE]),
        ("prepare", [*G, "ls-files"]),
        ("prepare", ["sh", "scripts/prepare-env.sh", "root", "backend", "tg_bot"]),
        ("preflight", status),
        ("preflight", [python, "-I", PROBE, "provenance", payload, KIT_COMMIT]),
        (
            "preflight",
            [
                kit,
                "check-install",
                package,
                "--json",
                *catalog,
                "--version",
                install["package"]["version"],
                "--product-root",
                ROOT,
            ],
        ),
        ("preflight", status),
        ("preflight", [python, "-I", PROBE, "preflight", payload, KIT_COMMIT]),
        ("preflight", status),
        ("package", [kit, "add", package, *catalog]),
        *(("library", [kit, "add", item["name"], *catalog]) for item in install["libraries"]),
        ("bind", [kit, "bind", package, "--default"]),
        ("generate", ["make", "generate-from-spec"]),
        ("validate", ["make", "validate-specs"]),
        ("validate", [f"{ROOT}/services/backend/.venv/bin/mypy", "services/backend"]),
        ("validate", [f"{ROOT}/services/tg_bot/.venv/bin/mypy", "services/tg_bot"]),
        ("validate", ["make", "tests", "REDIS_URL=redis://redis.invalid:6379"]),
        ("readback", [python, "-I", PROBE, "readback", payload, KIT_COMMIT]),
        ("commit", [*G, "add", "-A"]),
        ("commit", [*G, "status", "--porcelain"]),
        (
            "commit",
            [
                *G,
                "-c",
                "user.name=Codegen Bot",
                "-c",
                "user.email=codegen@localhost",
                "commit",
                "-m",
                f"Install catalog package {package} ({OPERATION})",
            ],
        ),
        ("commit", [*G, "rev-parse", "HEAD"]),
        ("push", heads),
        ("push", [*G, "push", "origin", f"HEAD:refs/heads/{branch}"]),
        ("push", heads),
    ]
    stages = [{"stage": stage, "argv": argv, "returncode": 0} for stage, argv in rows]
    index(stages, "check-install")["returncode"] = check_rc
    return stages


def index(stages, word):
    return next(row for row in stages if word in row["argv"])


def check(stages, typed=None, install=None, **identity):
    return check_execution(
        stages,
        preflight=preflight() if typed is None else typed,
        install=closure() if install is None else install,
        operation_id=identity.get("operation_id", OPERATION),
        story_id=STORY,
        checkout=identity.get("checkout", CHECKOUT),
        base_sha=identity.get("base_sha", BASE),
    )


def test_current_trace_with_closure_resolved_typed_glue_is_accepted():
    disposition = check(trace(closure()))
    assert disposition["accepted"] is True
    assert disposition["stages"] == [
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
    ]
    assert disposition["preflight"] == {
        "status": "glue",
        "returncode": 3,
        "resolved_by_closure": ["textparse"],
    }
    assert disposition["checkout"] == CHECKOUT and disposition["base_sha"] == BASE


def test_a_mechanical_package_without_libraries_runs_no_library_stage():
    install = closure("tg-channels", ())
    typed = preflight("tg-channels", glue=[])
    typed["target"]["tag"] = "packages/tg-channels/v0.5.0"
    disposition = check(trace(install, check_rc=0, remote_story=True), typed, install)
    assert "library" not in disposition["stages"]
    assert disposition["preflight"] == {
        "status": "mechanical",
        "returncode": 0,
        "resolved_by_closure": [],
    }


def drop_stage(name):
    return lambda stages: [row for row in stages if row["stage"] != name]


def at(word, key, value):
    def mutate(stages):
        index(stages, word)[key] = value
        return stages

    return mutate


def insert(position, row):
    def mutate(stages):
        stages.insert(position, row)
        return stages

    return mutate


@pytest.mark.parametrize(
    ("mutate", "reason"),
    [
        # The pre-prepare stage set the stand demanded before this repair is incomplete now.
        (drop_stage("prepare"), "every required stage"),
        (drop_stage("push"), "every required stage"),
        (drop_stage("library"), "every required stage"),
        (lambda stages: stages[:-1], "differs from the installer"),
        (
            insert(15, {"stage": "recover", "argv": ["make", "clean"], "returncode": 0}),
            "every required stage",
        ),
        (
            insert(20, {"stage": "validate", "argv": ["make", "lint"], "returncode": 0}),
            "differs from the installer",
        ),
        (at("push", "argv", ["git", "push", "origin", "HEAD:refs/heads/x"]), "hooks"),
        (
            at("push", "argv", [*G, "push", "--force", "origin", f"HEAD:refs/heads/story/{STORY}"]),
            "forced",
        ),
        (
            at("push", "argv", [*G, "push", "-f", "origin", f"HEAD:refs/heads/story/{STORY}"]),
            "forced",
        ),
        (at("generate-from-spec", "returncode", 2), "failed"),
        (at("bind", "returncode", 3), "failed"),
        (at("ls-files", "returncode", 1), "failed"),
        (at("check-install", "returncode", 0), "exit_mismatch"),
        (at("check-install", "returncode", 4), "exit_mismatch"),
        (at("check-install", "argv", ["kit", "check-install", "reminders"]), "differs"),
        (
            at(
                "scripts/prepare-env.sh",
                "argv",
                ["sh", "scripts/prepare-env.sh", "root", "backend"],
            ),
            "differs",
        ),
        (at("ls-files", "stage", "preflight"), "every required stage"),
        ({"stage": "push"}, "malformed"),
    ],
    ids=[
        "no-prepare",
        "no-push",
        "no-library-for-a-closure-library",
        "truncated",
        "extra-stage",
        "extra-command",
        "hooks",
        "force",
        "force-short",
        "failed-generate",
        "exit3-outside-preflight",
        "failed-prepare",
        "glue-exit0",
        "glue-exit4",
        "wrong-check-install-command",
        "partial-prepare-env",
        "prepare-command-moved",
        "malformed",
    ],
)
def test_an_unsafe_or_incomplete_trace_is_refused(mutate, reason):
    stages = trace(closure())
    stages = [mutate] if isinstance(mutate, dict) else mutate(stages)
    with pytest.raises(WitnessRefused, match=reason) as refused:
        check(stages)
    assert refused.value.disposition()["accepted"] is False


def test_a_failed_command_names_its_observed_stage_index_and_returncode():
    stages = at("make", "returncode", 2)(trace(closure()))
    with pytest.raises(WitnessRefused) as refused:
        check(stages)
    disposition = refused.value.disposition()
    assert (disposition["stage"], disposition["returncode"], disposition["argv"]) == (
        "generate",
        2,
        ["make", "generate-from-spec"],
    )
    assert stages[disposition["index"]]["argv"] == ["make", "generate-from-spec"]


def test_a_trace_too_short_to_reach_check_install_is_refused():
    install = closure("tg-channels", ())
    stages = [
        {"stage": stage, "argv": ["make", stage], "returncode": 0}
        for stage in ("prepare", "preflight", "package", "bind", "generate")
        + ("validate", "readback", "commit", "push")
    ]
    stages[0]["argv"] = [*G, "worktree", "add", "--detach", ROOT, BASE]
    with pytest.raises(WitnessRefused, match="fixed install probe"):
        check(stages, preflight("tg-channels", glue=[]), install)


def test_a_library_stage_outside_the_admitted_closure_is_refused():
    install = closure("tg-channels", ())
    stages = trace(closure("tg-channels", ("textparse",)), check_rc=0)
    typed = preflight("tg-channels", glue=[])
    typed["target"]["tag"] = "packages/tg-channels/v0.5.0"
    with pytest.raises(WitnessRefused, match="every required stage"):
        check(stages, typed, install)


def test_a_probe_on_another_closure_than_the_admitted_one_is_refused():
    other = closure()
    other["binding"]["sha256"] = "f" * 64
    stages = trace(other)
    with pytest.raises(WitnessRefused, match="differs from the installer"):
        check(stages)


@pytest.mark.parametrize(
    ("identity", "reason"),
    [
        ({"checkout": f"repo-e66cd313/install-{'0' * 32}"}, "operation-owned"),
        (
            {"operation_id": f"install-{'0' * 32}", "checkout": f"repo-x/install-{'0' * 32}"},
            "operation-owned",
        ),
        ({"base_sha": "9" * 40}, "admitted base"),
    ],
    ids=["foreign-checkout", "foreign-operation", "other-base"],
)
def test_preparation_outside_the_operations_checkout_or_base_is_refused(identity, reason):
    with pytest.raises(WitnessRefused, match=reason):
        check(trace(closure()), **identity)


def drifted(path, value):
    typed = preflight()
    target = typed
    *parents, leaf = path
    for key in parents:
        target = target[key]
    target[leaf] = value
    return typed


@pytest.mark.parametrize(
    ("typed", "check_rc", "reason"),
    [
        (None, 3, "no typed preflight"),
        ({"status": "glue"}, 3, "malformed"),
        ({**preflight(), "extra": True}, 3, "malformed"),
        (drifted(("target", "catalog_ref"), "0" * 40), 3, "target.catalog_ref"),
        (drifted(("target", "catalog_source"), "https://example.com/kit.git"), 3, "catalog_source"),
        (drifted(("target", "version"), "0.4.0"), 3, "target.version"),
        (drifted(("target", "tag"), "packages/reminders/v0.4.0"), 3, "target.tag"),
        (drifted(("product_core",), "2.4.0"), 3, "product_core"),
        (drifted(("package",), "tg-channels"), 3, "package"),
        (preflight(glue=[library_glue(symbol="dateparse")]), 3, "outstanding"),
        (preflight(glue=[library_glue(owner="package:tg-channels")]), 3, "outstanding"),
        (
            preflight(
                glue=[library_glue(), library_glue(code="binding_language_owner", owner="product")]
            ),
            3,
            "outstanding",
        ),
        (
            preflight(glue=[library_glue(code="command_collision", owner="product")]),
            3,
            "outstanding",
        ),
        (
            {
                **preflight(glue=[]),
                "status": "incompatible",
                "target": None,
                "product_core": None,
                "incompatible": {"code": "core_too_old", "explanation": "core 2.1"},
            },
            4,
            "incompatible",
        ),
        (preflight(glue=[]), 3, "exit_mismatch"),
    ],
    ids=[
        "missing",
        "malformed",
        "extra-field",
        "catalog-drift",
        "catalog-source-drift",
        "version-drift",
        "tag-drift",
        "core-drift",
        "package-drift",
        "absent-library",
        "foreign-owner",
        "product-language-glue",
        "product-command-glue",
        "incompatible",
        "mechanical-exit3",
    ],
)
def test_an_untrustworthy_typed_preflight_is_refused(typed, check_rc, reason):
    stages = trace(closure(), check_rc=check_rc)
    with pytest.raises(WitnessRefused, match=reason):
        check_execution(
            stages,
            preflight=typed,
            install=closure(),
            operation_id=OPERATION,
            story_id=STORY,
            checkout=CHECKOUT,
            base_sha=BASE,
        )


def test_a_closure_that_drifted_from_the_typed_result_is_refused():
    install = closure()
    install["catalog"]["commit"] = "0" * 40
    with pytest.raises(WitnessRefused, match="catalog_ref|differs"):
        check(trace(closure()), install=install)


def operation(**changes):
    return {
        "id": OPERATION,
        "story_id": STORY,
        "state": "published",
        "token": "operation-lease-token",
        "checkout": CHECKOUT,
        "base_sha": BASE,
        "head_sha": HEAD,
        "preflight": preflight(),
        **changes,
    }


def published(stages, **changes):
    return {
        "event": PUBLISHED_EVENT,
        "operation_id": OPERATION,
        "head_sha": HEAD,
        "execution_stages": stages,
        **changes,
    }


def test_publication_is_retained_before_it_is_accepted():
    artifact = {}
    stages = trace(closure())
    rows = [{"event": "other"}, published(stages)]
    assert witness_publication(artifact, rows, operation(), closure()) == stages
    retained = artifact["execution"]
    assert retained["stages"] == stages and retained["observations"] == 1
    assert retained["closure"] == closure() and retained["preflight"] == preflight()
    assert retained["witness"]["accepted"] is True
    assert "token" not in str(retained)


def test_a_refused_publication_keeps_the_actual_stages_and_the_precise_refusal():
    artifact = {}
    stages = trace(closure())
    stages[0:8] = []
    with pytest.raises(WitnessRefused):
        witness_publication(artifact, [published(stages)], operation(), closure())
    retained = artifact["execution"]
    assert retained["stages"] == stages
    assert retained["witness"]["accepted"] is False
    assert "every required stage" in retained["witness"]["reason"]
    assert "observed ['preflight'" in retained["witness"]["reason"]
    assert "token" not in str(retained)


@pytest.mark.parametrize(
    ("rows", "reason"),
    [
        ([], "found 0"),
        ([published([]), published([])], "found 2"),
        ([published([], operation_id="install-other")], "found 0"),
        ([published(trace(closure()), head_sha="0" * 40)], "published head differs"),
    ],
    ids=["none", "two", "other-operation", "other-head"],
)
def test_one_matching_publication_of_the_published_head_is_required(rows, reason):
    artifact = {}
    with pytest.raises(WitnessRefused, match=reason):
        witness_publication(artifact, rows, operation(), closure())
    assert artifact["execution"]["witness"]["accepted"] is False
    assert artifact["execution"]["observations"] == len(
        [row for row in rows if row["operation_id"] == OPERATION]
    )


def test_witness_input_is_not_mutated():
    stages = trace(closure())
    before = copy.deepcopy(stages)
    check(stages)
    assert stages == before


def tg_channels():
    install = closure("tg-channels", ())
    typed = preflight("tg-channels", glue=[])
    typed["target"]["tag"] = "packages/tg-channels/v0.5.0"
    return install, typed, trace(install, check_rc=0)


def runner_evidence():
    """The runner proof's retained witness input and disposition for both coexistence installs."""
    evidence = {"operations": {}, "installs": {}, "install_payloads": {}}
    for name, (install, typed, stages) in {
        "reminders": (closure(), preflight(), trace(closure())),
        "tg-channels": tg_channels(),
    }.items():
        evidence["install_payloads"][name] = install
        evidence["installs"][name] = {"stages": copy.deepcopy(stages)}
        evidence["operations"][name] = {
            "operation_id": OPERATION,
            "story_id": STORY,
            "checkout": CHECKOUT,
            "base_sha": BASE,
            "preflight": typed,
            "stand_witness": {
                "stages": stages,
                "disposition": check(stages, typed, install),
            },
        }
    return evidence


def test_the_runner_gate_accepts_each_legs_witnessed_native_install():
    assert stand_witness_problems(runner_evidence(), ["reminders", "tg-channels"]) == []


def forged_acceptance(evidence):
    for stages in (
        evidence["operations"]["reminders"]["stand_witness"]["stages"],
        evidence["installs"]["reminders"]["stages"],
    ):
        index(stages, "generate-from-spec")["returncode"] = 2


def reminders_without_exit3(evidence):
    """A reminders install that never reached check-install's library_required answer."""
    typed = preflight(glue=[])
    stages = trace(closure(), check_rc=0)
    operation = evidence["operations"]["reminders"]
    operation["preflight"] = typed
    operation["stand_witness"] = {"stages": stages, "disposition": check(stages, typed)}
    evidence["installs"]["reminders"]["stages"] = copy.deepcopy(stages)


@pytest.mark.parametrize(
    ("damage", "problem"),
    [
        (lambda e: e["operations"]["reminders"].pop("stand_witness"), "no stand witness"),
        (lambda e: e["operations"]["tg-channels"]["stand_witness"].pop("disposition"), "no stand"),
        (lambda e: e["installs"]["reminders"]["stages"].pop(), "not the executor's"),
        (forged_acceptance, "witness refused"),
        (reminders_without_exit3, "leg's path"),
        (lambda e: e["operations"]["reminders"].update(preflight=None), "witness refused"),
    ],
    ids=["missing", "skipped", "other-stages", "forged", "no-exit3", "no-typed-result"],
)
def test_the_runner_gate_fails_on_missing_skipped_or_refused_witness_evidence(damage, problem):
    evidence = runner_evidence()
    damage(evidence)
    problems = stand_witness_problems(evidence, ["reminders", "tg-channels"])
    assert len(problems) == 1 and problem in problems[0]


def test_the_runner_gate_names_a_package_without_a_witnessed_path():
    evidence = runner_evidence()
    evidence["operations"]["textparse"] = evidence["operations"].pop("reminders")
    evidence["installs"]["textparse"] = evidence["installs"].pop("reminders")
    evidence["install_payloads"]["textparse"] = evidence["install_payloads"].pop("reminders")
    assert stand_witness_problems(evidence, ["textparse"]) == [
        f"textparse: the witnessed install did not take its leg's path: "
        f"{evidence['operations']['textparse']['stand_witness']['disposition']}"
    ]
