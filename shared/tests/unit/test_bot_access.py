"""What the deployed bot does with the two values a QA run depends on.

Revocation is only real if the test identity is refused afterwards. The
orchestrator cannot ask the generated bot, so it keeps the template's admission
rule here, next to the audience parser it already shares, and checks the values
a deploy actually ships against it.
"""

from shared.contracts.bot_access import (
    QA_TEST_TELEGRAM_ID,
    bot_admits,
    parse_allowed_telegram_ids,
    project_bot_audience,
)

OWNER = 42


class TestAdmission:
    def test_empty_audience_is_the_public_bot(self):
        assert bot_admits(audience="", test_identity="", telegram_id=QA_TEST_TELEGRAM_ID)

    def test_private_bot_refuses_the_qa_identity_without_a_grant(self):
        assert not bot_admits(audience="42", test_identity="", telegram_id=QA_TEST_TELEGRAM_ID)

    def test_granted_test_slot_admits_exactly_the_qa_identity(self):
        assert bot_admits(
            audience="42",
            test_identity=str(QA_TEST_TELEGRAM_ID),
            telegram_id=QA_TEST_TELEGRAM_ID,
        )
        assert not bot_admits(
            audience="42", test_identity=str(QA_TEST_TELEGRAM_ID), telegram_id=999
        )

    def test_revoking_the_slot_denies_qa_and_leaves_the_owner_alone(self):
        """The value the revoke deploy ships, read the way the bot reads it."""
        revoked = {"audience": "42", "test_identity": ""}

        assert not bot_admits(**revoked, telegram_id=QA_TEST_TELEGRAM_ID)
        assert bot_admits(**revoked, telegram_id=OWNER)

    def test_malformed_test_slot_admits_nobody_extra(self):
        assert not bot_admits(
            audience="42", test_identity="not-an-id", telegram_id=QA_TEST_TELEGRAM_ID
        )


class TestAudienceReading:
    def test_audience_comes_from_the_project_s_bot_access(self):
        config = {"bot_access": {"mode": "custom", "allowed_telegram_ids": "42,43"}}
        assert parse_allowed_telegram_ids(project_bot_audience(config)) == {42, 43}

    def test_a_project_that_never_chose_an_audience_is_public(self):
        assert project_bot_audience({}) == ""
        assert project_bot_audience(None) == ""
