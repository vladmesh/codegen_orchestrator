"""An observation either says what it saw or says it saw nothing.

The type is what stops the two being confused. A caller closes out access on
"the slot is empty", so a result that failed to read anything must not be
constructible in a shape that reads as empty.
"""

import pytest

from shared.contracts.queues.env_observation import (
    EnvObservationOutcome,
    EnvObservationRequest,
    EnvObservationResult,
    env_observation_pending_key,
    env_observation_result_key,
)


def test_an_observation_says_whether_the_slot_is_filled():
    result = EnvObservationResult(
        request_id="envobs-1",
        outcome=EnvObservationOutcome.OBSERVED,
        env_key="TG_BOT_TEST_TELEGRAM_ID",
        present=False,
        containers=2,
    )
    assert result.present is False


def test_an_observation_without_a_reading_is_refused():
    with pytest.raises(ValueError):
        EnvObservationResult(
            request_id="envobs-1",
            outcome=EnvObservationOutcome.OBSERVED,
            env_key="TG_BOT_TEST_TELEGRAM_ID",
        )


def test_an_unreachable_service_cannot_also_report_what_it_holds():
    with pytest.raises(ValueError):
        EnvObservationResult(
            request_id="envobs-1",
            outcome=EnvObservationOutcome.UNREACHABLE,
            env_key="TG_BOT_TEST_TELEGRAM_ID",
            present=False,
            detail="ssh timed out",
        )


def test_an_unreachable_service_says_what_stopped_the_reading():
    with pytest.raises(ValueError):
        EnvObservationResult(
            request_id="envobs-1",
            outcome=EnvObservationOutcome.UNREACHABLE,
            env_key="TG_BOT_TEST_TELEGRAM_ID",
        )


def test_a_request_names_the_service_and_the_slot():
    request = EnvObservationRequest(
        request_id="envobs-1",
        project_id="p-1",
        server_handle="vps-1",
        service_slug="palindrome-bot",
        env_key="TG_BOT_TEST_TELEGRAM_ID",
    )
    assert request.request_id == "envobs-1"
    assert env_observation_result_key(request.request_id).endswith("envobs-1")
    assert env_observation_pending_key(request.request_id) != env_observation_result_key(
        request.request_id
    )
