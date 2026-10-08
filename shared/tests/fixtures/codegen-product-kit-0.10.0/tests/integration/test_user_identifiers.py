"""Prove 64-bit user persistence on disposable PostgreSQL databases."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from services.backend.src.app.models.setting import Setting, SettingScope
from services.backend.src.app.models.user import User, UserChannel, UserStatus
from services.backend.src.controllers.users import UsersController
from services.backend.src.core.settings import get_settings
from shared.generated.schemas import Status, UserGrant, UserRevoke

RELEASED_HEAD = "d4a7b2c9e1f0"
NEW_HEAD = "e6b8c2d4a901"
HIGH_ID = 2**31 + 1
NEXT_SMALL_ID = 8


@pytest.fixture
def database(monkeypatch: pytest.MonkeyPatch) -> Iterator[Engine]:
    """Create/drop only our randomly named database on the isolated integration server."""
    settings = get_settings()
    admin = create_engine(settings.sync_database_url, isolation_level="AUTOCOMMIT")
    assert admin.dialect.name == "postgresql", "This regression requires actual PostgreSQL"
    name = f"user_identifiers_{uuid4().hex}"
    with admin.connect() as connection:
        connection.exec_driver_sql(f'CREATE DATABASE "{name}"')
    url = admin.url.set(database=name)
    monkeypatch.setenv("DATABASE_URL", url.render_as_string(hide_password=False))
    get_settings.cache_clear()
    engine = create_engine(url)
    try:
        yield engine
    finally:
        engine.dispose()
        with admin.connect() as connection:
            connection.exec_driver_sql(f'DROP DATABASE "{name}" WITH (FORCE)')
        admin.dispose()
        get_settings.cache_clear()


def _migrate(target: str, *, downgrade: bool = False) -> None:
    config = Config(str(Path("services/backend/migrations/alembic.ini")))
    if downgrade:
        command.downgrade(config, target)
    else:
        command.upgrade(config, target)


def _types(engine: Engine) -> dict[tuple[str, str], str]:
    with engine.connect() as connection:
        return {
            (row.table_name, row.column_name): row.data_type
            for row in connection.execute(
                text(
                    "SELECT table_name, column_name, data_type FROM information_schema.columns "
                    "WHERE table_schema = 'public' AND table_name IN "
                    "('users', 'user_channels', 'settings')"
                )
            )
        }


def _snapshot(engine: Engine) -> dict[str, list[tuple]]:
    with engine.connect() as connection:
        return {
            table: [
                tuple(row)
                for row in connection.execute(
                    text(f"SELECT * FROM {table} ORDER BY id")  # noqa: S608 - fixed table inventory
                )
            ]
            for table in ("users", "user_channels", "settings")
        }


def _constraints(engine: Engine) -> list[tuple]:
    with engine.connect() as connection:
        return [
            tuple(row)
            for row in connection.execute(
                text(
                    "SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint "
                    "WHERE conrelid IN ('users'::regclass, 'user_channels'::regclass, "
                    "'settings'::regclass) ORDER BY conname"
                )
            )
        ]


def _seed(engine: Engine) -> None:
    with engine.begin() as connection:
        connection.execute(text("INSERT INTO users (id, status) VALUES (7, 'inactive')"))
        connection.execute(
            text(
                "INSERT INTO user_channels (id, user_id, channel, external_id) "
                "VALUES (9, 7, 'telegram', 'existing-identity')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO settings (id, key, scope, subject_id, value) VALUES "
                "(11, 'existing', 'user', 7, '{\"preserved\": true}'), "
                "(12, 'existing', 'product', 0, '{\"product\": true}')"
            )
        )
        connection.execute(text("SELECT setval('users_id_seq', 7, true)"))


def _assert_bigint(engine: Engine) -> None:
    types = _types(engine)
    for column in (("users", "id"), ("user_channels", "user_id"), ("settings", "subject_id")):
        assert types[column] == "bigint"
    for column in (("user_channels", "id"), ("settings", "id")):
        assert types[column] == "integer"
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == NEW_HEAD
        assert (
            connection.scalar(text("SELECT pg_get_serial_sequence('users', 'id')"))
            == "public.users_id_seq"
        )
        sequence = connection.execute(
            text(
                "SELECT data_type, max_value FROM pg_sequences WHERE sequencename = 'users_id_seq'"
            )
        ).one()
        assert tuple(sequence) == ("bigint", 2**63 - 1)


async def _store_and_resolve_high_ids(engine: Engine) -> None:
    async_engine = create_async_engine(engine.url.set(drivername="postgresql+asyncpg"))
    sessions = async_sessionmaker(async_engine, expire_on_commit=False)
    controller = UsersController()
    try:
        async with sessions.begin() as session:
            user = User(id=HIGH_ID, status=UserStatus.ACTIVE)
            session.add(UserChannel(user=user, channel="telegram", external_id=str(HIGH_ID)))
            session.add(
                Setting(
                    key="high", scope=SettingScope.USER, subject_id=HIGH_ID, value={"high": True}
                )
            )
        # Independent raw PostgreSQL readback, not the ORM identity map.
        with engine.connect() as connection:
            assert (
                connection.scalar(text("SELECT id FROM users WHERE id = :id"), {"id": HIGH_ID})
                == HIGH_ID
            )
            assert (
                connection.scalar(
                    text("SELECT user_id FROM user_channels WHERE external_id = :identity"),
                    {"identity": str(HIGH_ID)},
                )
                == HIGH_ID
            )
            assert connection.execute(
                text("SELECT subject_id, value FROM settings WHERE key = 'high'")
            ).one() == (HIGH_ID, {"high": True})
        # Prove the retained serial default generates high IDs without collisions or overflow.
        with engine.begin() as connection:
            connection.execute(text("SELECT setval('users_id_seq', :id, true)"), {"id": HIGH_ID})
        async with sessions.begin() as session:
            granted = await controller.grant(
                session, UserGrant(channel="telegram", external_id="8202532144")
            )
            assert granted.user_id == HIGH_ID + 1
            assert granted.external_id == "8202532144"
            assert granted.status == Status.active
        async with sessions.begin() as session:
            resolved = await controller.resolve(session, "telegram", "8202532144")
            assert resolved == granted
            revoked = await controller.revoke(
                session, UserRevoke(channel="telegram", external_id="8202532144")
            )
            assert revoked.status == Status.inactive
            assert revoked.user_id == granted.user_id
        async with sessions.begin() as session:
            assert (
                await controller.resolve(session, "telegram", "8202532144")
            ).status == Status.inactive
            assert (
                await controller.grant(
                    session, UserGrant(channel="telegram", external_id="8202532144")
                )
                == granted
            )
    finally:
        await async_engine.dispose()


def _assert_constraints_enforced(engine: Engine) -> None:
    invalid = [
        (
            "INSERT INTO user_channels (user_id, channel, external_id) "
            "VALUES (999, 'x', 'missing')",
            "user_channels_user_id_fkey",
        ),
        (
            "INSERT INTO user_channels (user_id, channel, external_id) "
            "VALUES (:id, 'telegram', :identity)",
            "uq_user_channels_channel_external_id",
        ),
        (
            "INSERT INTO settings (key, scope, subject_id, value) "
            "VALUES ('high', 'user', :id, 'null')",
            "uq_settings_key_scope_subject",
        ),
        (
            "INSERT INTO settings (key, scope, subject_id, value) "
            "VALUES ('invalid', 'product', :id, 'null')",
            "ck_settings_scope_subject",
        ),
        (
            "INSERT INTO settings (key, scope, subject_id, value) "
            "VALUES ('invalid', 'user', 0, 'null')",
            "ck_settings_scope_subject",
        ),
    ]
    for statement, constraint in invalid:
        with pytest.raises(IntegrityError, match=constraint), engine.begin() as connection:
            connection.execute(text(statement), {"id": HIGH_ID, "identity": str(HIGH_ID)})
    # FK cascade remains effective; settings retain their existing independent subject semantics.
    with engine.begin() as connection:
        connection.execute(text("DELETE FROM users WHERE id = :id"), {"id": HIGH_ID})
        assert (
            connection.scalar(
                text("SELECT count(*) FROM user_channels WHERE user_id = :id"), {"id": HIGH_ID}
            )
            == 0
        )
        assert (
            connection.scalar(text("SELECT subject_id FROM settings WHERE key = 'high'")) == HIGH_ID
        )


@pytest.mark.asyncio
async def test_fresh_full_chain_stores_high_user_columns(database: Engine) -> None:
    _migrate("head")
    _assert_bigint(database)
    await _store_and_resolve_high_ids(database)
    _assert_constraints_enforced(database)


@pytest.mark.asyncio
async def test_released_upgrade_preserves_data_constraints_and_sequence(database: Engine) -> None:
    _migrate(RELEASED_HEAD)
    types = _types(database)
    assert types[("users", "id")] == types[("user_channels", "user_id")] == "integer"
    assert types[("settings", "subject_id")] == "integer"
    _seed(database)
    before, constraints = _snapshot(database), _constraints(database)
    _migrate("head")
    _migrate("head")
    _assert_bigint(database)
    assert _snapshot(database) == before
    assert _constraints(database) == constraints
    with database.begin() as connection:
        assert (
            connection.scalar(text("INSERT INTO users DEFAULT VALUES RETURNING id"))
            == NEXT_SMALL_ID
        )
    await _store_and_resolve_high_ids(database)
    high_snapshot = _snapshot(database)
    with pytest.raises(RuntimeError, match="Cannot downgrade.*int32"):
        _migrate(RELEASED_HEAD, downgrade=True)
    _assert_bigint(database)
    assert _snapshot(database) == high_snapshot
    _assert_constraints_enforced(database)


def test_representable_downgrade_preserves_data_and_serial_default(database: Engine) -> None:
    _migrate(RELEASED_HEAD)
    _seed(database)
    before, constraints = _snapshot(database), _constraints(database)
    _migrate("head")
    _migrate(RELEASED_HEAD, downgrade=True)
    assert _snapshot(database) == before
    assert _constraints(database) == constraints
    assert _types(database)[("users", "id")] == "integer"
    assert _types(database)[("user_channels", "user_id")] == "integer"
    assert _types(database)[("settings", "subject_id")] == "integer"
    with database.begin() as connection:
        assert (
            connection.scalar(text("INSERT INTO users DEFAULT VALUES RETURNING id"))
            == NEXT_SMALL_ID
        )
        assert (
            connection.scalar(
                text("SELECT data_type FROM pg_sequences WHERE sequencename = 'users_id_seq'")
            )
            == "integer"
        )


@pytest.mark.parametrize("high_subject", [True, False])
def test_downgrade_refuses_high_settings_or_sequence_without_high_users(
    database: Engine, high_subject: bool
) -> None:
    _migrate("head")
    _seed(database)
    with database.begin() as connection:
        if high_subject:
            connection.execute(
                text(
                    "INSERT INTO settings (key, scope, subject_id, value) "
                    "VALUES ('high', 'user', :id, 'null')"
                ),
                {"id": HIGH_ID},
            )
        else:
            connection.execute(text("SELECT setval('users_id_seq', :id, true)"), {"id": HIGH_ID})
    before = _snapshot(database)
    with pytest.raises(RuntimeError, match="Cannot downgrade.*int32"):
        _migrate(RELEASED_HEAD, downgrade=True)
    assert _snapshot(database) == before
    _assert_bigint(database)
