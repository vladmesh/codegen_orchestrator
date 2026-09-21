"""A criterion outside QA's vocabulary is marked before QA and reported, never checked.

QA reads HTTP GET routes, sends Telegram text, presses inline buttons and fires
declared jobs. On 2026-09-15 the architect wrote `POST /api/transactions` and a
receipt photo upload as criteria, and QA failed them against any code. Only the
HTTP write is withheld before QA; an upload reaches the executor, which reports
it with its own `qa_capability` cause.
"""

from __future__ import annotations

import pytest

from shared.contracts.dto.run_result import QAFailedCheck, QAFailedCheckCause
from src.agents.qa.acceptance import prepare_central_qa_criteria
from src.consumers._qa_runner import QAResult, apply_unverifiable_criteria

POST_ITEM = "- POST /api/transactions with an amount returns 201"
PHOTO_ITEM = "- Receipt photo → OCR extracts the total and the bot records the expense"
TEXT_ITEM = '- Telegram: sending "coffee 250" replies with "Recorded 250"'
BUTTON_ITEM = "- Telegram: pressing the inline button Undo removes the last expense"
GET_ITEM = "- GET /api/transactions lists the recorded transaction"
SEPT_15_CRITERIA = "\n".join((POST_ITEM, PHOTO_ITEM, TEXT_ITEM, BUTTON_ITEM, GET_ITEM))


EXECUTOR = None

# Every line quoted by the 1290 reviews (rounds 5, 17 and 25) and the observer's
# decision, with what the pre-QA classifier must do with it. `None` is a line
# handed to the executor, which reports it itself if it cannot check it.
CLASSIFICATION_TABLE = [
    ("- POST http://localhost:8000/api/transactions returns 201", "http_write"),
    ("- POST to /api/transactions creates a transaction", "http_write"),
    (POST_ITEM, "http_write"),
    (PHOTO_ITEM, EXECUTOR),
    ("- Send a receipt photo to the bot", EXECUTOR),
    ("- Telegram: Upload a document to the bot", EXECUTOR),
    ("- Report file → sent to the admin chat every Monday", EXECUTOR),
    ("- Export file -> contains all transactions", EXECUTOR),
    ("- Daily document -> delivered at 09:00", EXECUTOR),
    ("- Bot: Send the report file to the bot owner", EXECUTOR),
    ("- GET /api/documents returns the document attached to the expense", EXECUTOR),
    ("- Telegram: /report replies with an attached PDF document", EXECUTOR),
    ("- Telegram: /files lists uploaded files", EXECUTOR),
    ("- Telegram: /delete /last removes it", EXECUTOR),
    ("- Sends a daily summary document at 09:00", EXECUTOR),
    ("- Sends the weekly report file to the user every Monday", EXECUTOR),
    ("- Forwarding a document link to the bot returns its title", EXECUTOR),
    ("- Send the document number to the bot and get its status", EXECUTOR),
    ("- The bot accepts a receipt photo and replies with the total", EXECUTOR),
    ("- Telegram: a photo of a receipt is recognised", EXECUTOR),
    ("- Telegram: send a voice note, bot transcribes", EXECUTOR),
    (TEXT_ITEM, EXECUTOR),
    (BUTTON_ITEM, EXECUTOR),
    (GET_ITEM, EXECUTOR),
]


@pytest.mark.parametrize(("line", "withheld_as"), CLASSIFICATION_TABLE)
def test_a_line_is_withheld_only_when_it_certainly_needs_an_http_write(line, withheld_as):
    prepared = prepare_central_qa_criteria(line)

    assert [adjustment.reason for adjustment in prepared.unverifiable] == (
        [] if withheld_as is EXECUTOR else [withheld_as]
    )
    assert prepared.criteria == ("" if withheld_as else line)


class TestTheSeptember15CriteriaSet:
    """Only the POST item is withheld; the receipt photo item goes to the executor.

    The photo item is not recognised before QA. It relies on the executor's own
    `qa_capability` cause: the executor cannot send a photo, so it fails that
    check with that cause instead of a product failure.
    """

    def test_only_the_post_item_is_marked_not_verifiable(self):
        prepared = prepare_central_qa_criteria(SEPT_15_CRITERIA)

        assert [(a.action, a.reason, a.original) for a in prepared.unverifiable] == [
            ("unverifiable", "http_write", POST_ITEM),
        ]
        assert POST_ITEM not in prepared.criteria

    def test_the_post_item_is_reported_as_qa_capability(self):
        unverifiable = prepare_central_qa_criteria(SEPT_15_CRITERIA).unverifiable

        result = apply_unverifiable_criteria(QAResult(passed=True, checks=[]), unverifiable)

        assert result.passed is False
        [check] = result.checks
        assert check["cause"] == QAFailedCheckCause.QA_CAPABILITY.value
        assert POST_ITEM.lstrip("- ") in check["name"]

    def test_the_photo_telegram_and_get_items_are_handed_to_the_executor(self):
        prepared = prepare_central_qa_criteria(SEPT_15_CRITERIA)

        assert prepared.criteria.splitlines() == [PHOTO_ITEM, TEXT_ITEM, BUTTON_ITEM, GET_ITEM]


class TestWhatIsOutsideTheVocabulary:
    def test_every_write_method_on_a_product_route_is_unverifiable(self):
        for line in (
            "- PUT /api/budgets/1 returns 200",
            "- PATCH `/api/v1/budgets/1` updates the limit",
            "- DELETE /api/transactions/7 returns 204",
        ):
            [adjustment] = prepare_central_qa_criteria(line).unverifiable
            assert adjustment.reason == "http_write", line

    def test_readable_evidence_is_not_mistaken_for_an_upload(self):
        criteria = "\n".join(
            (
                "- Telegram: /chart replies with a photo of this month's spending",
                "- GET /uploads/photos returns 200",
                "- GET /api/files -> 200",
                "- FIRE JOB weekly_report THEN GET /reports lists the uploaded photo count",
            )
        )

        prepared = prepare_central_qa_criteria(criteria)

        assert prepared.adjustments == ()
        assert prepared.criteria == criteria

    def test_platform_owned_settings_and_jobs_lines_keep_their_adjustments(self):
        prepared = prepare_central_qa_criteria(
            "\n".join(
                (
                    "- POST /settings/set returns 200",
                    "- POST /api/jobs/fire returns 200 THEN GET /digests exposes records",
                )
            )
        )

        assert [(a.action, a.reason) for a in prepared.adjustments] == [
            ("dropped", "settings_seed_readback"),
            ("rewritten", "jobs_fire_transport"),
        ]
        assert prepared.unverifiable == ()


class TestAnUnverifiableCriterionInTheRunResult:
    def test_a_run_that_only_carried_unverifiable_criteria_does_not_pass(self):
        unverifiable = prepare_central_qa_criteria(SEPT_15_CRITERIA).unverifiable
        verdict = QAResult(
            passed=True, checks=[{"name": "text reply", "pass": True, "detail": "ok"}]
        )

        result = apply_unverifiable_criteria(verdict, unverifiable)

        assert result.passed is False
        # Mapped the way the QA consumer stores a failed check on the run.
        failed = [
            QAFailedCheck.model_validate({k: check[k] for k in ("name", "detail", "cause")})
            for check in result.checks
            if not check["pass"]
        ]
        assert [check.cause for check in failed] == [QAFailedCheckCause.QA_CAPABILITY]
        assert POST_ITEM.lstrip("- ") in failed[0].name
        assert "not verifiable" in result.summary

    def test_a_product_failure_keeps_its_cause_and_its_summary(self):
        unverifiable = prepare_central_qa_criteria(POST_ITEM).unverifiable
        verdict = QAResult(
            passed=False,
            checks=[{"name": "GET list", "pass": False, "detail": "404", "cause": "product"}],
            summary="the list route is missing",
        )

        result = apply_unverifiable_criteria(verdict, unverifiable)

        assert [check["cause"] for check in result.checks] == ["product", "qa_capability"]
        assert result.summary.startswith("the list route is missing; ")

    def test_a_run_with_nothing_unverifiable_is_returned_unchanged(self):
        verdict = QAResult(passed=True, checks=[], summary="OK")

        assert apply_unverifiable_criteria(verdict, ()) is verdict
        assert verdict.passed is True
        assert verdict.summary == "OK"
