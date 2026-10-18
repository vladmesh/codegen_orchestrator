import json

import pytest

from shared.contracts.queues.scheduler_wake import (
    MAX_WAKE_HINT_CHARS,
    SchedulerWakeHint,
    SchedulerWakeHintRejected,
    SchedulerWakeTarget,
    parse_scheduler_wake_hint,
)


@pytest.mark.parametrize(
    ("target", "wire_target"),
    [
        (SchedulerWakeTarget.STORY_COMPLETION, "story_completion"),
        (SchedulerWakeTarget.TASK_DISPATCHER, "task_dispatcher"),
        (SchedulerWakeTarget.LIFECYCLE_SUPERVISION, "lifecycle_supervision"),
    ],
)
def test_a_published_hint_parses_back_to_its_target(target, wire_target):
    wire = SchedulerWakeHint(target=target).model_dump_json()

    assert json.loads(wire) == {"target": wire_target}
    assert parse_scheduler_wake_hint(wire).target is target


@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        ('{"target": "pr_ci"}', "invalid"),
        ('{"target": "STORY_COMPLETION"}', "invalid"),
        ('{"target": "TASK_DISPATCHER"}', "invalid"),
        ('{"target": "LIFECYCLE_SUPERVISION"}', "invalid"),
        ('{"target": 0}', "invalid"),
        ("{}", "invalid"),
        ('{"target": "story_completion", "story_id": "story-1"}', "invalid"),
        ("not json", "invalid"),
        (b'{"target": "story_completion"}', "not_text"),
        (1, "not_text"),
        ('{"target": "story_completion"' + " " * MAX_WAKE_HINT_CHARS + "}", "oversized"),
    ],
)
def test_a_payload_outside_the_contract_is_rejected_not_rerouted(payload, reason):
    with pytest.raises(SchedulerWakeHintRejected) as rejected:
        parse_scheduler_wake_hint(payload)

    assert rejected.value.diagnostics["reason"] == reason


def test_rejection_diagnostics_never_echo_the_payload():
    secret = "ghp_" + "x" * 36
    payload = json.dumps({"target": secret})

    with pytest.raises(SchedulerWakeHintRejected) as rejected:
        parse_scheduler_wake_hint(payload)

    assert secret not in repr(rejected.value.diagnostics)
    assert secret not in str(rejected.value)
