"""Whole-config writes (POST/PUT/PATCH) can't smuggle a bot token or rewrite secrets."""

from types import SimpleNamespace

from fastapi import HTTPException
import pytest

from src.routers.projects import _vet_config_write

BOT_TOKEN = "123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw"  # noqa: S105
HTTP_UNPROCESSABLE = 422


def _project(config: dict | None):
    return SimpleNamespace(config=config)


@pytest.mark.parametrize(
    "config",
    [
        {"secrets": {"TELEGRAM_BOT_TOKEN": BOT_TOKEN}},
        {"secrets": {"INNOCENT": BOT_TOKEN}},
        {"env": {"TELEGRAM_BOT_TOKEN": "anything"}},
        {"env": {"SOME_KEY": BOT_TOKEN}},
        {"notes": [{"value": BOT_TOKEN}]},
    ],
    ids=["secrets-known-key", "secrets-disguised", "nested-key", "nested-value", "inside-a-list"],
)
def test_config_write_with_token_material_is_refused(config):
    with pytest.raises(HTTPException) as exc:
        _vet_config_write(config, None)
    assert exc.value.status_code == HTTP_UNPROCESSABLE


def test_secrets_blob_cannot_be_replaced():
    project = _project({"secrets": {"KEY_A": "enc-a"}})

    with pytest.raises(HTTPException) as exc:
        _vet_config_write({"secrets": {"KEY_A": "enc-a", "KEY_B": "enc-b"}}, project)
    assert exc.value.status_code == HTTP_UNPROCESSABLE


def test_stored_secrets_survive_a_config_write_that_omits_them():
    project = _project({"secrets": {"KEY_A": "enc-a"}, "modules": ["backend"]})

    stored = _vet_config_write({"tree": "src/"}, project)

    assert stored == {"tree": "src/", "secrets": {"KEY_A": "enc-a"}}


def test_unchanged_secrets_blob_round_trips():
    project = _project({"secrets": {"KEY_A": "enc-a"}})

    stored = _vet_config_write({"secrets": {"KEY_A": "enc-a"}, "tree": "src/"}, project)

    assert stored == {"tree": "src/", "secrets": {"KEY_A": "enc-a"}}


def test_env_hints_may_name_the_token_but_not_contain_one():
    hints = {"TELEGRAM_BOT_TOKEN": "Telegram bot token from @BotFather"}

    assert _vet_config_write({"env_hints": hints}, None) == {"env_hints": hints}

    with pytest.raises(HTTPException) as exc:
        _vet_config_write({"env_hints": {"TELEGRAM_BOT_TOKEN": BOT_TOKEN}}, None)
    assert exc.value.status_code == HTTP_UNPROCESSABLE


def test_plain_config_passes_through():
    assert _vet_config_write({"modules": ["backend"], "description": "bot"}, None) == {
        "modules": ["backend"],
        "description": "bot",
    }
