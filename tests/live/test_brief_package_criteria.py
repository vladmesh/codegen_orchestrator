"""The package variant's criteria, read by the platform that will judge them.

Offline, and worth its own file: the paid run of `mega-brief-package` can only
pass if the line the variant asks the architect to publish survives both halves
of the contract central QA applies to a package behaviour — the parser that
takes the behaviour's name and arguments off it, and the binder that decides
which read of the deployed product answers its observable.  A criterion phrased
"THEN the owner receives the reminder" parses fine and then fails its row by
design, so parsing alone is not the check.
"""

from __future__ import annotations

from pipeline_helpers import (
    BRIEF_DIGEST_SCENARIO,
    BRIEF_JOB_NAME,
    BRIEF_PACKAGE_ACCEPTANCE_CRITERION,
    BRIEF_PACKAGE_JOB_ARGUMENT,
    BRIEF_PACKAGE_JOB_NAME,
    BRIEF_PACKAGE_OWNER_REF,
    BRIEF_PACKAGE_REMINDER_STATE,
    BRIEF_PACKAGE_ROUTE,
    BRIEF_PACKAGE_SCENARIO,
    BRIEF_PACKAGE_SETTINGS_KEY,
    BRIEF_PACKAGE_TICK_AT,
    brief_package_detailed_spec,
)
import pytest

from services.langgraph.src.agents.qa.packages import observable_paths, observation_answers
from shared.contracts.acceptance import parse_scheduled_behaviours

pytestmark = pytest.mark.needs_no_api_credential


def _behaviour():
    behaviours = parse_scheduled_behaviours(BRIEF_PACKAGE_ACCEPTANCE_CRITERION)
    assert [one.name for one in behaviours] == [BRIEF_PACKAGE_JOB_NAME]
    return behaviours[0]


def test_the_criterion_names_the_package_behaviour_and_its_required_argument():
    """`reminders.tick` is not argument-free: its schema requires `at`."""
    behaviour = _behaviour()

    assert behaviour.arguments == {BRIEF_PACKAGE_JOB_ARGUMENT: BRIEF_PACKAGE_TICK_AT}


def test_the_criterion_observable_binds_a_read_of_the_package_route():
    behaviour = _behaviour()

    assert observable_paths(behaviour.observable) == (BRIEF_PACKAGE_ROUTE,)
    assert observation_answers(
        behaviour.observable,
        "http_get",
        f"{BRIEF_PACKAGE_ROUTE}?user_ref={BRIEF_PACKAGE_OWNER_REF}",
    )
    assert BRIEF_PACKAGE_REMINDER_STATE in behaviour.observable


def test_a_dispatch_record_and_an_unrelated_read_answer_nothing():
    """The fire and its receipt are not the observation the row rests on."""
    behaviour = _behaviour()

    assert not observation_answers(behaviour.observable, "fire_job", BRIEF_PACKAGE_JOB_NAME)
    assert not observation_answers(behaviour.observable, "http_get", "/health")


def test_a_bot_only_observable_would_fail_the_row_by_design():
    """The counter-example this variant is written against, stated once."""
    bot_only = "the owner receives the reminder"

    assert observable_paths(bot_only) == ()
    assert not observation_answers(bot_only, "http_get", BRIEF_PACKAGE_ROUTE)


def test_the_variant_asks_the_architect_for_exactly_this_line():
    """The published criteria come from the brief, so the brief carries the line."""
    assert BRIEF_PACKAGE_ACCEPTANCE_CRITERION in brief_package_detailed_spec()
    assert BRIEF_PACKAGE_SCENARIO.job_name == BRIEF_PACKAGE_JOB_NAME


def test_the_variant_uses_the_package_owned_setting_seed_contract():
    detailed_spec = brief_package_detailed_spec()

    assert BRIEF_PACKAGE_SETTINGS_KEY == "reminders.reminder_owner_ref"
    assert BRIEF_PACKAGE_SCENARIO.settings_key == BRIEF_PACKAGE_SETTINGS_KEY
    assert "package's declared setting seed" in detailed_spec
    assert "POST /settings/set" in detailed_spec
    assert "POST /settings/get" in detailed_spec
    assert "settings.reminder_owner_ref" not in detailed_spec
    assert (
        "Declare it in the generated\nbackend service manifest's settings_schema"
        not in detailed_spec
    )
    assert "DB trigger" in detailed_spec
    assert "startup poll" in detailed_spec
    assert "product-owned seed" in detailed_spec


def test_the_two_variants_expect_different_behaviour_shapes():
    """Neither variant's expectation is asserted for the other."""
    behaviour = _behaviour()
    package_error = BRIEF_PACKAGE_SCENARIO.criterion_error(behaviour)

    assert package_error is None
    assert BRIEF_PACKAGE_JOB_NAME != BRIEF_JOB_NAME
    assert behaviour.arguments != {}
    # The digest variant's expectation, applied to this behaviour, refuses it —
    # which is why it is stated per variant and not once for both.
    assert BRIEF_DIGEST_SCENARIO.criterion_error(behaviour) is not None


def test_each_variant_hands_the_run_evidence_the_expectation_it_was_judged_on():
    """One expectation, applied twice: by the fixture, then by the artifact.

    The evidence document used to spell the digest variant's job name and
    arguments itself, which is why a green `mega-brief-package` run was called
    red (`issue:62bc9840e23a44c2098b`). It now reads what the scenario declared,
    and this is that declaration being the same terms the fixture refuses on.
    """
    assert BRIEF_DIGEST_SCENARIO.expected_criterion == {
        "name": BRIEF_JOB_NAME,
        "required_arguments": [],
        "allows_other_arguments": False,
    }
    assert BRIEF_PACKAGE_SCENARIO.expected_criterion == {
        "name": BRIEF_PACKAGE_JOB_NAME,
        "required_arguments": [BRIEF_PACKAGE_JOB_ARGUMENT],
        "allows_other_arguments": True,
    }
    # Only the variant that asks something of its deployment owes the package
    # route it read off it.
    assert "package_route" in BRIEF_PACKAGE_SCENARIO.evidence_obligations
    assert "package_route" not in BRIEF_DIGEST_SCENARIO.evidence_obligations
