"""The consumers that write product registry secrets refuse to start without their policy."""

from unittest.mock import MagicMock

from pydantic import ValidationError
import pytest

from src.consumers import deploy, engineering


@pytest.mark.parametrize("consumer", [deploy, engineering], ids=["deploy", "engineering"])
def test_a_registry_writer_without_the_lease_policy_never_starts(monkeypatch, consumer):
    monkeypatch.delenv("GITHUB_REGISTRY_SECRETS_LEASE_SECONDS", raising=False)
    start_worker = MagicMock()
    monkeypatch.setattr(consumer, "start_worker", start_worker)

    with pytest.raises(ValidationError, match="github_registry_secrets_lease_seconds"):
        consumer.main()

    start_worker.assert_not_called()


@pytest.mark.parametrize("consumer", [deploy, engineering], ids=["deploy", "engineering"])
def test_a_registry_writer_with_its_policy_starts(monkeypatch, consumer):
    monkeypatch.setenv("GITHUB_REGISTRY_SECRETS_LEASE_SECONDS", "180")
    start_worker = MagicMock()
    monkeypatch.setattr(consumer, "start_worker", start_worker)

    consumer.main()

    start_worker.assert_called_once()
