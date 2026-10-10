"""What an operation may be told, and what its retained evidence may say."""

from __future__ import annotations

import json

import pytest

from src.synthetic_buyer.config import (
    ConfigError,
    MissingSecret,
    SecretHandle,
    load_config,
    parse_config,
    resolve_secret,
)
from src.synthetic_buyer.evidence import (
    REQUIRED_OBSERVATIONS,
    EvidenceStore,
    ObservationStatus,
    Phase,
    Redaction,
    new_record,
)
from tests.unit.synthetic_buyer.fakes import REVISION, TOKEN, config_data


def test_a_complete_config_names_every_identity_and_bound():
    config = parse_config(config_data())

    assert config.scenario.public_channels == ["chan_one", "chan_two"]
    assert config.codegen_bot.user_id == 7001
    assert (
        config.secret_handles()["product_token.handle"].describe() == "env:BUYER_PRODUCT_BOT_TOKEN"
    )


@pytest.mark.parametrize(
    "missing", ["codegen_bot", "buyer", "model", "deadlines", "api", "platform"]
)
def test_an_identity_or_policy_left_out_is_a_refusal_not_a_default(missing):
    data = config_data()
    del data[missing]

    with pytest.raises(ConfigError, match=missing):
        parse_config(data)


def test_a_refusal_names_the_field_but_never_quotes_its_value():
    data = config_data(api={"base_url": f"https://user:{TOKEN}@api"})

    with pytest.raises(ConfigError) as refused:
        parse_config(data)

    assert "api.base_url" in str(refused.value)
    assert TOKEN not in str(refused.value)


def test_only_russian_is_ordered_and_only_english_switched_to():
    scenario = config_data()["scenario"] | {"product_language": "en"}

    with pytest.raises(ConfigError, match="scenario.product_language"):
        parse_config(config_data(scenario=scenario))


def test_botfather_mode_names_its_bot_and_takes_no_handle():
    token = {"mode": "botfather", "botfather_username": "BotFather", "bot_display_name": "Каналы"}
    assert parse_config(config_data(product_token=token)).product_token.handle is None

    with pytest.raises(ConfigError, match="product_token"):
        parse_config(config_data(product_token=token | {"handle": {"env": "X"}}))


def test_a_handle_is_one_place_and_an_empty_value_is_missing(tmp_path):
    with pytest.raises(ValueError, match="exactly one"):
        SecretHandle(env="A", file="/b")
    handle = SecretHandle(env="BUYER_TOKEN")

    with pytest.raises(MissingSecret, match="env:BUYER_TOKEN is unset or empty"):
        resolve_secret(handle, {"BUYER_TOKEN": "  "})
    with pytest.raises(MissingSecret, match="cannot be read"):
        resolve_secret(SecretHandle(file=str(tmp_path / "absent")), {})
    (tmp_path / "token").write_text(TOKEN + "\n")
    assert resolve_secret(SecretHandle(file=str(tmp_path / "token")), {}) == TOKEN


def test_a_config_file_that_is_not_json_is_refused_without_its_content(tmp_path):
    path = tmp_path / "buyer.json"
    path.write_text(TOKEN)

    with pytest.raises(ConfigError, match="not JSON") as refused:
        load_config(path)
    assert TOKEN not in str(refused.value)


def test_announced_values_and_credential_shapes_are_scrubbed_everywhere():
    redaction = Redaction()
    redaction.add("promo-value-0123456789")
    key = "cps_abcdefghijk2_" + "A" * 43

    scrubbed = redaction.value(
        {
            "text": f"code promo-value-0123456789, token {TOKEN}, key {key}",
            "nested": [{"envelope": "gAAAAA" + "b" * 40}],
            TOKEN: "as a key",
        }
    )

    serialized = json.dumps(scrubbed)
    for secret in ("promo-value-0123456789", TOKEN, key, "gAAAAA" + "b" * 40):
        assert secret not in serialized


def _store(tmp_path) -> EvidenceStore:
    store = EvidenceStore(tmp_path, Redaction())
    store.record = new_record(
        operation_id="op-1", revision=REVISION, handles={}, now="2026-10-10T00:00:00+00:00"
    )
    return store


def test_the_verdict_passes_only_when_every_required_fact_was_observed(tmp_path):
    store = _store(tmp_path)
    for name in REQUIRED_OBSERVATIONS:
        store.observe(name, ObservationStatus.OBSERVED, provenance="test")

    assert store.conclude() == "passed"


def test_an_unknown_fact_is_incomplete_and_a_failed_one_names_its_phase(tmp_path):
    store = _store(tmp_path)
    for name in REQUIRED_OBSERVATIONS:
        store.observe(name, ObservationStatus.OBSERVED, provenance="test")
    store.observe("post_delivered", ObservationStatus.UNKNOWN, provenance="test")

    assert store.conclude() == "incomplete"

    store.observe("reader_usage", ObservationStatus.FAILED, provenance="test")
    assert store.conclude() == "failed"
    assert store.record["verdict"]["failure_phase"] == Phase.PLATFORM.value


def test_a_recorded_failure_is_never_overwritten(tmp_path):
    store = _store(tmp_path)
    store.fail(Phase.ORDER, "conversation_stalled")
    for name in REQUIRED_OBSERVATIONS:
        store.observe(name, ObservationStatus.OBSERVED, provenance="test")

    store.fail(Phase.BUILD, "build_timeout")

    assert store.conclude() == "failed"
    assert store.record["verdict"]["reason"] == "conversation_stalled"
    report = (tmp_path / "report.md").read_text()
    assert "conversation_stalled" in report and "`order`" in report


def test_the_live_record_keeps_working_while_the_retained_one_is_redacted(tmp_path):
    store = _store(tmp_path)
    store.redaction.add(TOKEN)
    section = store.record.setdefault("order", {})
    section["echo"] = TOKEN
    store.save()
    section["turns"] = 1
    store.save()

    retained = EvidenceStore(tmp_path, Redaction()).load()
    assert retained["order"]["turns"] == 1
    assert TOKEN not in json.dumps(retained)
