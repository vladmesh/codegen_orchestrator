"""The Product Brief vocabulary refuses the shapes the boundary cannot mean.

Fast, no database: what is checked here is that the types themselves make an
ambiguous disposition, a duplicate requirement id or a blank requirement
impossible to express. The durable behaviour — the idempotent admission, the
attempt fence — is proved against a real database in
`services/api/tests/service/test_product_brief_admission.py`.
"""

from pydantic import ValidationError
import pytest

from shared.contracts.dto.engineering_dispatch import (
    OVERRIDABLE_REFUSALS,
    EngineeringDispatchRefusal,
)
from shared.contracts.dto.product_brief import (
    InitialSetting,
    ProductBriefAdmissionOutcome,
    ProductBriefAdmissionRead,
    ProductBriefConfirm,
    ProductBriefContent,
    ProductBriefCreate,
    ProposedProductBriefContent,
    RequirementCoverageCreate,
    SettingScope,
)


def _content(**overrides) -> dict:
    base = {
        "summary": "A bot that tracks reading",
        "must_requirements": [{"id": "r1", "text": "It stores a book"}],
    }
    base.update(overrides)
    return base


class TestProductBriefContent:
    def test_minimal(self):
        content = ProductBriefContent.model_validate(_content())
        assert [r.id for r in content.must_requirements] == ["r1"]

    def test_a_brief_with_no_must_requirement_is_not_a_brief(self):
        """Coverage over an empty requirement set would admit vacuously."""
        with pytest.raises(ValidationError):
            ProductBriefContent.model_validate(_content(must_requirements=[]))

    def test_duplicate_requirement_ids_are_refused(self):
        """Requirements are addressed by id, so two rows cannot share one."""
        with pytest.raises(ValidationError):
            ProductBriefContent.model_validate(
                _content(
                    must_requirements=[
                        {"id": "r1", "text": "one"},
                        {"id": "r1", "text": "two"},
                    ]
                )
            )

    def test_a_blank_requirement_is_refused(self):
        with pytest.raises(ValidationError):
            ProductBriefContent.model_validate(
                _content(must_requirements=[{"id": "  ", "text": "one"}])
            )

    def test_unknown_content_fields_are_refused(self):
        """Confirmed content is hashed, so a silently dropped field is a lie."""
        with pytest.raises(ValidationError):
            ProductBriefContent.model_validate(_content(extra="smuggled"))


class TestStoredContentKeepsParsing:
    """The read shape is what `ProductBriefRead` pulls out of the JSON column.

    Everything this card added to the document is optional there, so a revision
    the released API stored before any of it existed still parses as the same
    brief — with nothing seeded and no provenance recorded.
    """

    def test_a_document_written_before_the_new_fields_still_parses(self):
        content = ProductBriefContent.model_validate(_content())
        assert content.initial_settings == []
        assert content.must_requirements[0].user_wording is None
        assert content.must_requirements[0].wording_reference is None

    def test_a_stored_id_that_is_not_path_safe_still_reads(self):
        """Reading is not the boundary that refuses it; opening a revision is."""
        content = ProductBriefContent.model_validate(
            _content(must_requirements=[{"id": "a/b", "text": "one"}])
        )
        assert content.must_requirements[0].id == "a/b"


def _proposed(**overrides) -> dict:
    base = {
        "summary": "A bot that tracks reading",
        "must_requirements": [
            {"id": "r1", "text": "It stores a book", "user_wording": "remember my books"}
        ],
    }
    base.update(overrides)
    return base


class TestProposedContentIsPathSafe:
    """A must-requirement id is one path segment of the coverage route.

    `PUT /product-briefs/{id}/coverage/{requirement_id}` is how the architect
    disposes of a requirement. An id carrying a `/` addresses another route, so
    the disposition would come back as a 404 that says nothing about why. The
    refusal belongs where the revision is opened, not there.
    """

    @pytest.mark.parametrize("bad_id", ["a/b", "../escape", "a b", "a%2Fb", "a?x", ".hidden"])
    def test_a_path_significant_id_is_refused(self, bad_id):
        with pytest.raises(ValidationError):
            ProposedProductBriefContent.model_validate(
                _proposed(
                    must_requirements=[{"id": bad_id, "text": "one", "user_wording": "words"}]
                )
            )

    def test_the_create_dto_is_where_that_refusal_happens(self):
        with pytest.raises(ValidationError):
            ProductBriefCreate(
                project_id="6c8f0f6a-1f9b-4c0e-9f9c-2a0d2b3c4d5e",
                title="Reading tracker",
                content=_proposed(
                    must_requirements=[{"id": "r/1", "text": "one", "user_wording": "words"}]
                ),
                request_id="po-brief:p:r1",
            )

    @pytest.mark.parametrize("good_id", ["r1", "stores-a-book", "req.1", "a_b"])
    def test_an_addressable_id_is_accepted(self, good_id):
        content = ProposedProductBriefContent.model_validate(
            _proposed(must_requirements=[{"id": good_id, "text": "one", "user_wording": "words"}])
        )
        assert content.must_requirements[0].id == good_id


class TestProposedContentCarriesTheUsersWording:
    """What the user asked for is auditable, or the requirement is a paraphrase."""

    def test_neither_wording_nor_reference_is_refused(self):
        with pytest.raises(ValidationError):
            ProposedProductBriefContent.model_validate(
                _proposed(must_requirements=[{"id": "r1", "text": "one"}])
            )

    def test_both_at_once_is_refused(self):
        """Two answers to the one question of where the requirement came from."""
        with pytest.raises(ValidationError):
            ProposedProductBriefContent.model_validate(
                _proposed(
                    must_requirements=[
                        {
                            "id": "r1",
                            "text": "one",
                            "user_wording": "words",
                            "wording_reference": "telegram:chat=1:message=2",
                        }
                    ]
                )
            )

    def test_a_reference_is_a_provenance(self):
        content = ProposedProductBriefContent.model_validate(
            _proposed(
                must_requirements=[
                    {
                        "id": "r1",
                        "text": "one",
                        "wording_reference": "telegram:chat=1:message=2",
                    }
                ]
            )
        )
        assert content.must_requirements[0].wording_reference == "telegram:chat=1:message=2"

    def test_a_blank_wording_is_not_a_wording(self):
        with pytest.raises(ValidationError):
            ProposedProductBriefContent.model_validate(
                _proposed(must_requirements=[{"id": "r1", "text": "one", "user_wording": "  "}])
            )

    def test_the_confirmation_echoes_the_same_strict_document(self):
        """Confirming is echoing the stored revision, so it is the write shape."""
        with pytest.raises(ValidationError):
            ProductBriefConfirm(
                request_id="po-brief-confirm:brief-1",
                content=_proposed(must_requirements=[{"id": "r1", "text": "one"}]),
            )


class TestInitialSettings:
    """The typed seed values, in the vocabulary the generated product uses."""

    def test_settings_are_ordered_and_typed(self):
        content = ProposedProductBriefContent.model_validate(
            _proposed(
                initial_settings=[
                    {"key": "alerts.default_currency", "value": "USD"},
                    {"key": "alerts.digest_hour", "value": 9},
                ]
            )
        )
        assert [s.key for s in content.initial_settings] == [
            "alerts.default_currency",
            "alerts.digest_hour",
        ]
        assert content.initial_settings[0].scope is SettingScope.PRODUCT
        assert content.initial_settings[1].value == 9

    def test_the_same_key_scope_and_subject_twice_is_refused(self):
        """Two values for one identity is not an initial state."""
        with pytest.raises(ValidationError):
            ProposedProductBriefContent.model_validate(
                _proposed(
                    initial_settings=[
                        {"key": "alerts.default_currency", "value": "USD"},
                        {"key": "alerts.default_currency", "value": "EUR"},
                    ]
                )
            )

    def test_a_user_scoped_value_names_its_subject(self):
        setting = InitialSetting(key="alerts.digest_hour", scope="user", subject_id=7, value=9)
        assert setting.subject_id == 7
        with pytest.raises(ValidationError):
            InitialSetting(key="alerts.digest_hour", scope="user", value=9)

    def test_a_product_scoped_value_has_no_subject(self):
        with pytest.raises(ValidationError):
            InitialSetting(key="alerts.digest_hour", scope="product", subject_id=7, value=9)

    def test_a_subject_id_is_positive(self):
        with pytest.raises(ValidationError):
            InitialSetting(key="alerts.digest_hour", scope="user", subject_id=0, value=9)

    def test_a_key_the_product_could_not_declare_is_refused(self):
        with pytest.raises(ValidationError):
            InitialSetting(key="Alerts Default", value="USD")

    @pytest.mark.parametrize(
        "key", ["telegram.bot_token", "openrouter.api_key", "admin.password", "app.secret"]
    )
    def test_a_credential_key_is_not_a_setting(self, key):
        """A secret is resolved by Python at the execution boundary, never here."""
        with pytest.raises(ValidationError):
            InitialSetting(key=key, value="whatever")

    def test_a_credential_shaped_value_is_not_a_setting(self):
        """The brief is read back by the architect, and therefore by an LLM."""
        with pytest.raises(ValidationError):
            InitialSetting(key="bot.greeting", value="123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw")


class TestRequirementCoverageCreate:
    def test_a_covering_task_is_a_disposition(self):
        coverage = RequirementCoverageCreate(
            requirement_id="r1", planning_attempt_id="plan-1", task_id="task-1"
        )
        assert coverage.task_id == "task-1"

    def test_a_returned_reason_is_a_disposition(self):
        coverage = RequirementCoverageCreate(
            requirement_id="r1", planning_attempt_id="plan-1", returned_reason="out of scope"
        )
        assert coverage.returned_reason == "out of scope"

    def test_no_disposition_is_refused(self):
        with pytest.raises(ValidationError):
            RequirementCoverageCreate(requirement_id="r1", planning_attempt_id="plan-1")

    def test_two_dispositions_are_refused(self):
        """Covered *and* returned is two answers to one question."""
        with pytest.raises(ValidationError):
            RequirementCoverageCreate(
                requirement_id="r1",
                planning_attempt_id="plan-1",
                task_id="task-1",
                returned_reason="out of scope",
            )

    def test_coverage_names_the_attempt_that_wrote_it(self):
        """No default: a disposition that named no attempt could not be superseded."""
        with pytest.raises(ValidationError):
            RequirementCoverageCreate(requirement_id="r1", task_id="task-1")


class TestProductBriefAdmissionRead:
    def test_an_incomplete_admission_releases_nothing(self):
        answer = ProductBriefAdmissionRead(
            brief_id="brief-1",
            story_id="story-1",
            outcome=ProductBriefAdmissionOutcome.INCOMPLETE,
            missing_requirement_ids=["r2"],
        )
        assert answer.released_task_ids == []


def test_the_brief_refusal_is_not_overridable():
    """An operator may not buy a worker for a plan the architect has not finished.

    Every other rung-1 refusal is a judgement about one task an operator can be
    authorised to make. This one is a property of the whole plan: the tasks are
    released together, by the brief's admission step, or not at all.
    """
    assert EngineeringDispatchRefusal.PRODUCT_BRIEF_NOT_ADMITTED not in OVERRIDABLE_REFUSALS
