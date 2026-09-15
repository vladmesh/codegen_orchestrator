"""A criterion outside QA's vocabulary is marked before QA and reported, never checked.

QA reads HTTP GET routes, sends Telegram text, presses inline buttons and fires
declared jobs. On 2026-09-15 the architect wrote `POST /api/transactions` and a
receipt photo upload as criteria, and QA failed them against any code.
"""

from __future__ import annotations

from shared.contracts.dto.run_result import QAFailedCheck, QAFailedCheckCause
from src.agents.qa.acceptance import prepare_central_qa_criteria
from src.consumers._qa_runner import QAResult, apply_unverifiable_criteria

POST_ITEM = "- POST /api/transactions with an amount returns 201"
PHOTO_ITEM = "- Receipt photo → OCR extracts the total and the bot records the expense"
TEXT_ITEM = '- Telegram: sending "coffee 250" replies with "Recorded 250"'
BUTTON_ITEM = "- Telegram: pressing the inline button Undo removes the last expense"
GET_ITEM = "- GET /api/transactions lists the recorded transaction"
SEPT_15_CRITERIA = "\n".join((POST_ITEM, PHOTO_ITEM, TEXT_ITEM, BUTTON_ITEM, GET_ITEM))


class TestTheSeptember15CriteriaSet:
    def test_the_post_and_photo_items_are_marked_not_verifiable(self):
        prepared = prepare_central_qa_criteria(SEPT_15_CRITERIA)

        assert [(a.action, a.reason, a.original) for a in prepared.unverifiable] == [
            ("unverifiable", "http_write", POST_ITEM),
            ("unverifiable", "telegram_media_upload", PHOTO_ITEM),
        ]
        assert POST_ITEM not in prepared.criteria
        assert PHOTO_ITEM not in prepared.criteria

    def test_the_telegram_and_get_items_stay_checks_handed_to_the_executor(self):
        prepared = prepare_central_qa_criteria(SEPT_15_CRITERIA)

        assert prepared.criteria.splitlines() == [TEXT_ITEM, BUTTON_ITEM, GET_ITEM]


class TestWhatIsOutsideTheVocabulary:
    def test_every_write_method_on_a_product_route_is_unverifiable(self):
        for line in (
            "- PUT /api/budgets/1 returns 200",
            "- PATCH `/api/v1/budgets/1` updates the limit",
            "- delete /api/transactions/7 returns 204",
        ):
            [adjustment] = prepare_central_qa_criteria(line).unverifiable
            assert adjustment.reason == "http_write", line

    def test_a_file_or_media_sent_to_the_bot_is_unverifiable(self):
        for line in (
            "- Upload a PDF document and the bot confirms it",
            "- The user sends a voice message to the bot and gets a transcript",
            "- Attaching a photo to /expense stores it",
        ):
            [adjustment] = prepare_central_qa_criteria(line).unverifiable
            assert adjustment.reason == "telegram_media_upload", line

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
        assert [check.cause for check in failed] == [QAFailedCheckCause.QA_CAPABILITY] * 2
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
