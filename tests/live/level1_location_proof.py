"""What `mega-live`'s first QA Run has to show for the QA sandbox to count as proven.

Under `mega-live` the level-1 bot answers a native Telegram location with its
coordinates (`level1_change_set.level1_location_criterion`), and the first
story's QA checklist asks the real QA executor to judge exactly that. A `passed`
QA Run alone does not prove the sandbox: an executor that could not send a
location reports the check as unverified, and the runner then passes the Run
with the check taken out of the verdict. So the proof is three facts of the one
Run the QA consumer settled, all read off its own result as the suite records it
into evidence (`run_evidence.qa_run_facts`) — the record the artifact carries, so
the assertion and the artifact cannot disagree:

* the Run is `passed`;
* `passed_checks` names the location check, and `unverified_checks` does not;
* `probe_runs` retains a Telegram probe the executor ran in its sandbox — the
  `telegram/location` library seed or its own script — that exited 0, whose
  source sends a native geo point (Telethon's `InputGeoPoint`), and whose output
  shows the bot's reply carrying the rounded coordinates.

The check names are the executor's own words, so the location check is found by
what it is about rather than by one exact spelling. The reply is found in the
seed's JSON replies when the output is that JSON, and in the raw output
otherwise; the coordinates QA is asked to send never contain the rounded pair
(`LEVEL1_LOCATION_LATITUDE`), so a probe echoing what it sent cannot stand in for
the bot's answer.

`location_proof_mismatches` is the one predicate: the live assertion and the
offline regressions judge the same thing, and every entry is one missing fact,
phrased so the failed assertion can print it as it stands.
"""

from __future__ import annotations

import json

from level1_change_set import (
    LEVEL1_LOCATION_REPLY_LATITUDE,
    LEVEL1_LOCATION_REPLY_LONGITUDE,
)

from shared.contracts.dto.run_result import QAOutcome, QAProbePlatform

#: What a check name says when it is the location check, in the languages an
#: executor judging a Russian-language product names its checks in.
LOCATION_CHECK_WORDS = ("location", "geo", "coordinat", "локац", "геопоз", "координат")
#: Telethon's input type for a native geo point. `InputMediaGeoPoint` wraps it,
#: so a source that sends one names this type either way.
GEO_POINT_INPUT_TYPE = "InputGeoPoint"
#: How much of a probe's stderr or an unverified reason a mismatch quotes.
QUOTE_LIMIT = 400


def is_location_check(name: str) -> bool:
    """Whether a check name, as the executor wrote it, is about the location."""
    folded = name.casefold()
    return any(word in folded for word in LOCATION_CHECK_WORDS)


def _quote(text: str | None) -> str:
    text = (text or "").strip()
    if len(text) > QUOTE_LIMIT:
        text = text[:QUOTE_LIMIT] + "…"
    return repr(text)


def _reply_texts(stdout: str) -> list[str]:
    """The bot's replies a probe printed, or its whole output when it printed no JSON.

    The seed prints one JSON object whose `replies` hold the bot's messages
    beside the coordinates it sent; only the replies are the bot's words. A
    script of the executor's own may print anything, so its output is read as a
    whole.
    """
    texts: list[str] = []
    printed_json = False
    for line in stdout.splitlines():
        try:
            document = json.loads(line)
        except ValueError:
            continue
        if not isinstance(document, dict) or not isinstance(document.get("replies"), list):
            continue
        printed_json = True
        for reply in document["replies"]:
            if isinstance(reply, dict):
                texts.extend(
                    value
                    for value in (reply.get("text"), reply.get("caption"))
                    if isinstance(value, str)
                )
    return texts if printed_json else [stdout]


def _shows_the_coordinates(stdout: str) -> bool:
    return any(
        LEVEL1_LOCATION_REPLY_LATITUDE in text and LEVEL1_LOCATION_REPLY_LONGITUDE in text
        for text in _reply_texts(stdout)
    )


def _sends_a_geo_point(source: str) -> bool:
    return GEO_POINT_INPUT_TYPE in source


def location_probe_runs(record: dict) -> list[dict]:
    """The retained probes that prove the location check, in call order."""
    return [
        probe
        for probe in record.get("probe_runs") or []
        if probe.get("platform") == QAProbePlatform.TELEGRAM.value
        and probe.get("exit_status") == 0
        and _sends_a_geo_point(probe.get("source") or "")
        and _shows_the_coordinates(probe.get("stdout") or "")
    ]


def _probe_account(probe: dict) -> str:
    """What one Telegram probe did, for a reader of a failed assertion."""
    missing = []
    if probe.get("exit_status") != 0:
        missing.append(f"exited {probe.get('exit_status')}")
    if not _sends_a_geo_point(probe.get("source") or ""):
        missing.append(f"its source sends no {GEO_POINT_INPUT_TYPE}")
    if not _shows_the_coordinates(probe.get("stdout") or ""):
        missing.append(
            f"its output shows no reply carrying {LEVEL1_LOCATION_REPLY_LATITUDE} and "
            f"{LEVEL1_LOCATION_REPLY_LONGITUDE}"
        )
    return (
        f"{probe.get('name')!r} ({'; '.join(missing)}; stdout {_quote(probe.get('stdout'))}; "
        f"stderr {_quote(probe.get('stderr'))})"
    )


def _probe_mismatches(record: dict) -> list[str]:
    probes = record.get("probe_runs")
    if probes is None:
        return [
            "the QA Run carries no probe record (probe_runs is null), so no probe the executor "
            "ran in its sandbox is retained"
        ]
    if location_probe_runs(record):
        return []
    telegram = [
        probe for probe in probes if probe.get("platform") == QAProbePlatform.TELEGRAM.value
    ]
    if not telegram:
        return [
            "probe_runs holds no telegram probe: the executor sent the bot no location from "
            f"its sandbox (probes retained: {[probe.get('name') for probe in probes]})"
        ]
    return [
        "probe_runs holds no telegram probe that exited 0, sends a native geo point and shows "
        "the bot's reply carrying the coordinates: "
        + "; ".join(_probe_account(probe) for probe in telegram)
    ]


def location_proof_mismatches(record: dict) -> list[str]:
    """Why this QA Run does not prove the location check, if it does not.

    `record` is the QA Run as `run_evidence.qa_run_facts` records it: its id,
    its outcome and the verification facts and probe records of its
    `QARunResult`. An empty list is the proof.
    """
    reasons: list[str] = []
    outcome = record.get("qa_outcome")
    if outcome != QAOutcome.PASSED.value:
        reasons.append(
            f"QA Run {record.get('id')} ended qa_outcome={outcome!r}, not passed "
            f"(summary {_quote(record.get('summary'))}, error {_quote(record.get('error'))})"
        )
    unverified = [
        check
        for check in record.get("unverified_checks") or []
        if is_location_check(check.get("name") or "")
    ]
    for check in unverified:
        reasons.append(
            f"the location check {check.get('name')!r} is in unverified_checks "
            f"({check.get('origin')}): {_quote(check.get('reason'))}"
        )
    passed = record.get("passed_checks") or []
    if not any(is_location_check(name) for name in passed):
        reasons.append(f"passed_checks names no location check: {passed}")
    reasons.extend(_probe_mismatches(record))
    return reasons
