"""Offline regressions for `mega-live`'s proof of the QA sandbox.

`location_proof_mismatches` is what `TestFullPipeline.test_qa_passed` asserts on
the first story's QA Run under `mega-live`. It is fed here the Run a green proof
leaves — a passed Run whose executor sent the bot a native location with the
`telegram/location` library seed — and then that Run with each fact taken away
in turn. A predicate that answered "no reasons" to any of those would let a
`passed` Run whose executor never reached Telegram stand as the proof.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

from level1_change_set import (
    LEVEL1_LOCATION_LATITUDE,
    LEVEL1_LOCATION_LONGITUDE,
    LEVEL1_LOCATION_REPLY_LATITUDE,
    LEVEL1_LOCATION_REPLY_LONGITUDE,
    level1_location_criterion,
)
from level1_location_proof import (
    LocationRefusal,
    accepted_location_refusals,
    location_probe_runs,
    location_proof_mismatches,
)
import pipeline_helpers
import pytest
from run_evidence import qa_run_facts

from shared.contracts.dto.run_result import QARunResult

pytestmark = pytest.mark.needs_no_api_credential

REPO_ROOT = Path(__file__).resolve().parents[2]
SEED_SOURCE = (REPO_ROOT / "shared/qa_probe_library/telegram/location.py").read_text(
    encoding="utf-8"
)
BOT = "@mega_e2e_codegen_bot"
REPLY = f"location: {LEVEL1_LOCATION_REPLY_LATITUDE}, {LEVEL1_LOCATION_REPLY_LONGITUDE}"
LOCATION_CHECK = "bot answers a native location with its rounded coordinates"


def _mismatches(run: dict) -> list[str]:
    """The predicate, over the record the suite keeps of this Run."""
    return location_proof_mismatches(qa_run_facts(run))


def _seed_stdout(replies: list[dict]) -> str:
    """What the seed prints: the location it sent and the bot's replies, as JSON."""
    return (
        json.dumps(
            {
                "action": "location",
                "bot": BOT,
                "latitude": float(LEVEL1_LOCATION_LATITUDE),
                "longitude": float(LEVEL1_LOCATION_LONGITUDE),
                "delivered": True,
                "sent_message_id": 41,
                "replies": replies,
                "error": None,
            },
            ensure_ascii=False,
        )
        + "\n"
    )


def _seed_probe(**overrides) -> dict:
    probe = {
        "id": "probe-1",
        "platform": "telegram",
        "name": "location",
        "source": SEED_SOURCE,
        "arguments": [BOT, LEVEL1_LOCATION_LATITUDE, LEVEL1_LOCATION_LONGITUDE, "15"],
        "stdout": _seed_stdout(
            [{"id": 42, "text": REPLY, "caption": None, "media_type": None, "buttons": []}]
        ),
        "stderr": "",
        "exit_status": 0,
        "duration_ms": 16200,
        "source_truncated": False,
        "stdout_truncated": False,
        "stderr_truncated": False,
        "file_kind": "py",
    }
    probe.update(overrides)
    return probe


def _passed_run() -> dict:
    """The first story's QA Run a green `mega-live` proof leaves behind."""
    return {
        "id": "qa-poll-1380",
        "status": "completed",
        "result": {
            "qa_outcome": "passed",
            "summary": "all checks passed",
            "failed_checks": [],
            "passed_checks": [
                "health endpoint answers 200",
                "GET /level1/marker carries this run's marker",
                LOCATION_CHECK,
            ],
            "unverified_checks": [],
            "probe_runs": [
                {
                    **_seed_probe(),
                    "id": "probe-0",
                    "platform": "http",
                    "name": "marker",
                    "source": "curl -s $URL/level1/marker\n",
                    "stdout": "{}",
                    "file_kind": "sh",
                },
                _seed_probe(),
            ],
            "probe_library": {
                "offered": [{"platform": "telegram", "name": "location", "origin": "seed"}]
            },
        },
    }


def test_the_canned_run_is_a_valid_qa_run_result():
    """The fixture is the shape the QA consumer serializes, not a guess at it."""
    QARunResult.model_validate(_passed_run()["result"])


def test_a_passed_run_whose_seed_probe_got_the_coordinates_back_is_the_proof():
    run = _passed_run()

    assert _mismatches(run) == []
    assert [probe["id"] for probe in location_probe_runs(qa_run_facts(run))] == ["probe-1"]


def test_an_executor_s_own_script_that_sends_a_geo_point_is_the_proof_too():
    """The seed is one way; a script of the executor's own is the other."""
    run = _passed_run()
    run["result"]["probe_runs"] = [
        _seed_probe(
            name="send_location",
            source=(
                "from telethon.tl.types import InputGeoPoint, InputMediaGeoPoint\n"
                "...client.send_file(bot, InputMediaGeoPoint(InputGeoPoint(lat, long)))\n"
            ),
            stdout=f"bot replied: {REPLY}\n",
        )
    ]

    assert _mismatches(run) == []


def test_a_run_that_did_not_pass_is_named_with_its_summary():
    run = _passed_run()
    run["result"]["qa_outcome"] = "failed"
    run["result"]["summary"] = "the bot ignored the location"

    reasons = _mismatches(run)

    assert len(reasons) == 1
    assert "qa_outcome='failed', not passed" in reasons[0]
    assert "the bot ignored the location" in reasons[0]


def test_an_unverified_location_check_is_named_with_its_reason():
    """The Run passes with the check taken out of the verdict: not a proof."""
    run = _passed_run()
    run["result"]["passed_checks"].remove(LOCATION_CHECK)
    run["result"]["unverified_checks"] = [
        {
            "name": "Location reply",
            "reason": "no tool sends a native location to the bot",
            "origin": "executor",
        }
    ]

    reasons = _mismatches(run)

    assert reasons == [
        "the location check 'Location reply' is in unverified_checks (executor): "
        "'no tool sends a native location to the bot'",
        "passed_checks names no location check: "
        "['health endpoint answers 200', \"GET /level1/marker carries this run's marker\"]",
    ]


def test_a_location_check_both_passed_and_unverified_is_still_refused():
    run = _passed_run()
    run["result"]["unverified_checks"] = [
        {"name": "геолокация", "reason": "transport refused", "origin": "not_applicable"}
    ]

    reasons = _mismatches(run)

    assert len(reasons) == 1
    assert "'геолокация' is in unverified_checks" in reasons[0]


def test_passed_checks_naming_no_location_check_is_named():
    run = _passed_run()
    run["result"]["passed_checks"] = ["health endpoint answers 200"]

    assert _mismatches(run) == [
        "passed_checks names no location check: ['health endpoint answers 200']"
    ]


def test_a_run_with_no_probe_record_is_named():
    run = _passed_run()
    run["result"]["probe_runs"] = None

    reasons = _mismatches(run)

    assert len(reasons) == 1
    assert "probe_runs is null" in reasons[0]


def test_a_run_whose_probes_never_reached_telegram_is_named():
    run = _passed_run()
    run["result"]["probe_runs"] = run["result"]["probe_runs"][:1]

    reasons = _mismatches(run)

    assert len(reasons) == 1
    assert "probe_runs holds no telegram probe" in reasons[0]
    assert "'marker'" in reasons[0]


def test_a_telegram_probe_that_failed_is_named_with_its_stderr():
    run = _passed_run()
    run["result"]["probe_runs"][1] = _seed_probe(
        exit_status=3,
        stdout="",
        stderr="location probe refused: no QA Telegram identity at ~/.qa/telegram_identity.json",
    )

    reasons = _mismatches(run)

    assert len(reasons) == 1
    assert "exited 3" in reasons[0]
    assert "no QA Telegram identity" in reasons[0]


def test_a_telegram_probe_that_sent_text_not_a_geo_point_is_named():
    """Coordinates typed as text are not a native location."""
    run = _passed_run()
    run["result"]["probe_runs"][1] = _seed_probe(
        name="typed_coordinates",
        source="client.send_message(bot, '55.75588 37.61738')\n",
        stdout=f"bot replied: {REPLY}\n",
    )

    reasons = _mismatches(run)

    assert len(reasons) == 1
    assert "its source sends no InputGeoPoint" in reasons[0]


def test_a_probe_that_only_echoes_what_it_sent_is_not_the_bot_s_reply():
    """The seed prints the location it sent; with the bot silent, that is all it prints."""
    run = _passed_run()
    run["result"]["probe_runs"][1] = _seed_probe(stdout=_seed_stdout([]))

    reasons = _mismatches(run)

    assert len(reasons) == 1
    assert (
        f"its output shows no reply carrying {LEVEL1_LOCATION_REPLY_LATITUDE} and "
        f"{LEVEL1_LOCATION_REPLY_LONGITUDE}"
    ) in reasons[0]


def test_a_reply_with_unrounded_coordinates_is_not_the_behaviour():
    run = _passed_run()
    run["result"]["probe_runs"][1] = _seed_probe(
        stdout=_seed_stdout(
            [
                {
                    "id": 42,
                    "text": f"location: {LEVEL1_LOCATION_LATITUDE}, {LEVEL1_LOCATION_LONGITUDE}",
                    "caption": None,
                    "media_type": None,
                    "buttons": [],
                }
            ]
        )
    )

    assert _mismatches(run) != []


def test_the_sent_coordinates_never_contain_the_rounded_reply():
    """What makes an echo distinguishable from the bot's answer."""
    assert LEVEL1_LOCATION_REPLY_LATITUDE not in LEVEL1_LOCATION_LATITUDE
    assert LEVEL1_LOCATION_REPLY_LONGITUDE not in LEVEL1_LOCATION_LONGITUDE
    assert LEVEL1_LOCATION_REPLY_LATITUDE not in str(float(LEVEL1_LOCATION_LATITUDE))
    assert LEVEL1_LOCATION_REPLY_LONGITUDE not in str(float(LEVEL1_LOCATION_LONGITUDE))
    assert f"{round(float(LEVEL1_LOCATION_LATITUDE), 4)}" == LEVEL1_LOCATION_REPLY_LATITUDE
    assert f"{round(float(LEVEL1_LOCATION_LONGITUDE), 4)}" == LEVEL1_LOCATION_REPLY_LONGITUDE


def test_the_location_criterion_names_what_the_proof_checks():
    line = level1_location_criterion()

    assert line.startswith("- ") and "\n" not in line
    assert "native Telegram location" in line
    for value in (
        LEVEL1_LOCATION_LATITUDE,
        LEVEL1_LOCATION_LONGITUDE,
        LEVEL1_LOCATION_REPLY_LATITUDE,
        LEVEL1_LOCATION_REPLY_LONGITUDE,
    ):
        assert value in line


def test_the_qa_run_evidence_retains_the_probe_records():
    """What the paid run's artifact shows: the probe, its source and its output."""
    run = _passed_run()
    ctx: dict = {}

    pipeline_helpers.record_qa_run(ctx, copy.deepcopy(run))

    # Retained as the Run holds it, through the same redaction every retained
    # control-plane payload goes through.
    record = ctx["qa_run_record"]
    expected = pipeline_helpers.redacted_payload(run["result"])
    assert record["passed_checks"] == expected["passed_checks"]
    assert record["unverified_checks"] == []
    assert record["probe_runs"] == expected["probe_runs"]
    retained = [probe for probe in record["probe_runs"] if probe["platform"] == "telegram"]
    assert [probe["id"] for probe in retained] == ["probe-1"]
    assert "InputGeoPoint" in retained[0]["source"]
    assert REPLY in retained[0]["stdout"]
    assert record["probe_library"] == expected["probe_library"]
    # And the live assertion judges exactly that retained record.
    assert location_proof_mismatches(record) == []


# ── The one accepted not-applicable check: the seed's own argument refusal ──

OUT_OF_RANGE_CHECK = "Telegram location with out-of-range latitude"
SEED_REFUSAL = "LAT must be within [-90, 90], got '999'"
#: The reason the QA runner wrote on run 36309935451, around the executor's detail.
OUT_OF_RANGE_REASON = (
    "reported not applicable, but this run recorded no transport refusal for it: "
    f'probe-2 refused before sending: "{SEED_REFUSAL}" '
    "(exit 2, argument refused before reaching the bot)"
)


def _refused_probe(**overrides) -> dict:
    """The seed refusing a latitude of 999 before it sent anything, as run 36309935451 kept it."""
    return _seed_probe(
        **{
            "id": "probe-2",
            "arguments": [BOT, "999", LEVEL1_LOCATION_LONGITUDE, "5"],
            "stdout": "",
            "stderr": f"location probe refused: {SEED_REFUSAL}\n",
            "exit_status": 2,
            "duration_ms": 106,
            **overrides,
        }
    )


def _out_of_range_run(*, reason: str = OUT_OF_RANGE_REASON, **probe_overrides) -> dict:
    """Run 36309935451's first QA Run: the in-range check passed, the out-of-range one refused."""
    run = _passed_run()
    run["result"]["unverified_checks"] = [
        {"name": OUT_OF_RANGE_CHECK, "reason": reason, "origin": "not_applicable"}
    ]
    run["result"]["probe_runs"].append(_refused_probe(**probe_overrides))
    return run


def test_the_out_of_range_run_is_a_valid_qa_run_result():
    QARunResult.model_validate(_out_of_range_run()["result"])


def test_the_seed_s_argument_refusal_of_the_named_value_is_accepted_and_recorded():
    """Run 36309935451's shape: green, and the evidence says which refusal was accepted."""
    record = qa_run_facts(_out_of_range_run())

    assert location_proof_mismatches(record) == []
    assert accepted_location_refusals(record) == [
        {
            "check": OUT_OF_RANGE_CHECK,
            "refusal": LocationRefusal.ARGUMENT.value,
            "probe_id": "probe-2",
            "invalid_value": "999",
            "exit_status": 2,
            "message": SEED_REFUSAL,
        }
    ]
    # The in-range check is still proven by its own successful probe.
    assert [probe["id"] for probe in location_probe_runs(record)] == ["probe-1"]


def test_a_longitude_refusal_is_the_same_acceptance():
    message = "LON must be within [-180, 180], got '-181.5'"
    run = _out_of_range_run(
        reason=f"not applicable: the probe refused {message}",
        arguments=[BOT, LEVEL1_LOCATION_LATITUDE, "-181.5"],
        stderr=f"location probe refused: {message}\n",
    )
    run["result"]["unverified_checks"][0]["name"] = "location with out-of-range longitude"

    assert _mismatches(run) == []


def _refused(run: dict) -> list[str]:
    reasons = _mismatches(run)
    assert accepted_location_refusals(qa_run_facts(run)) == []
    assert len(reasons) == 1, reasons
    assert f"the location check {OUT_OF_RANGE_CHECK!r} is in unverified_checks" in reasons[0]
    return reasons


def test_a_generic_not_applicable_with_no_refusal_is_refused():
    run = _out_of_range_run()
    run["result"]["probe_runs"].pop()

    reasons = _refused(run)

    assert "no retained telegram probe shows the seed's own argument refusal" in reasons[0]


def test_an_argument_refusal_of_a_different_value_is_refused():
    """The check names 999; the probe the seed refused was sent 1000."""
    run = _out_of_range_run(
        arguments=[BOT, "1000", LEVEL1_LOCATION_LONGITUDE, "5"],
        stderr="location probe refused: LAT must be within [-90, 90], got '1000'\n",
    )

    _refused(run)


def test_a_refusal_line_that_names_another_value_than_the_arguments_is_refused():
    """The stderr says 999, but the arguments the probe record retains are 1000."""
    _refused(_out_of_range_run(arguments=[BOT, "1000", LEVEL1_LOCATION_LONGITUDE, "5"]))


def test_a_value_that_only_contains_the_refused_one_is_not_naming_it():
    _refused(_out_of_range_run(reason=f"{OUT_OF_RANGE_REASON.split(':')[0]}: latitude 9990"))


def test_a_refusal_with_any_other_exit_status_is_refused():
    _refused(_out_of_range_run(exit_status=1))


def test_a_refusal_the_seed_would_not_print_is_refused():
    _refused(_out_of_range_run(stderr="LAT out of range\n"))


def test_an_argument_refusal_that_is_not_about_the_range_is_refused():
    message = "LAT must be a decimal number, got 'abc'"
    _refused(
        _out_of_range_run(
            reason=f"refused: {message}",
            arguments=[BOT, "abc", LEVEL1_LOCATION_LONGITUDE],
            stderr=f"location probe refused: {message}\n",
        )
    )


def test_a_refusal_by_a_probe_that_sends_no_geo_point_is_refused():
    _refused(_out_of_range_run(source="print('hello')\n"))


def test_only_a_not_applicable_origin_is_accepted():
    run = _out_of_range_run()
    run["result"]["unverified_checks"][0]["origin"] = "executor"

    _refused(run)


def test_only_the_out_of_range_location_check_is_accepted():
    """The in-range check is never excused by a refusal, however well grounded."""
    run = _out_of_range_run()
    run["result"]["unverified_checks"][0]["name"] = LOCATION_CHECK

    reasons = _mismatches(run)

    assert accepted_location_refusals(qa_run_facts(run)) == []
    assert len(reasons) == 1
    assert f"the location check {LOCATION_CHECK!r} is in unverified_checks" in reasons[0]


def test_an_accepted_refusal_does_not_stand_in_for_the_in_range_pass():
    run = _out_of_range_run()
    run["result"]["passed_checks"].remove(LOCATION_CHECK)

    reasons = _mismatches(run)

    assert len(reasons) == 1
    assert reasons[0].startswith("passed_checks names no location check")


def test_an_accepted_refusal_does_not_stand_in_for_the_in_range_probe():
    run = _out_of_range_run()
    run["result"]["probe_runs"] = [
        probe for probe in run["result"]["probe_runs"] if probe["id"] != "probe-1"
    ]

    reasons = _mismatches(run)

    assert len(reasons) == 1
    assert reasons[0].startswith("probe_runs holds no telegram probe that exited 0")
