"""One credential-safe administrator alert episode per host-session executor."""

import asyncio
from datetime import UTC, datetime, timedelta
import json

from fakeredis import aioredis
import pytest

from shared.contracts.dto.executor_diagnostics import (
    PROFILE_CONDITION_OUTCOMES,
    CredentialExpirySource,
    ExecutorAuthMode,
    ExecutorDiagnostic,
    ExecutorDiagnosticSnapshot,
    ExecutorProfileAlertEpisode,
    ExecutorProfileAlertOutcome,
    ExecutorProfileAlertState,
    ExecutorProfileCondition,
    ExecutorProfileObservation,
    ProfileLoginState,
    RefreshMaterialState,
    executor_profile_alert_key,
    safe_executor_diagnostic_reason,
)
from shared.contracts.vocab import AgentType
from shared.notifications import AdminDeliveryResult
from shared.tests.executor_diagnostic_cases import host_profile
from src.profile_alerts import (
    ALERT_RETRY_MAX,
    ExecutorProfileAlerts,
    alert_message,
    alert_retry_delay,
)

T0 = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)
DELIVERED = AdminDeliveryResult(configured=2, succeeded=2)
PARTIAL = AdminDeliveryResult(configured=2, succeeded=1)
FAILED = AdminDeliveryResult(configured=2, succeeded=0)
UNADDRESSABLE = AdminDeliveryResult(configured=0, succeeded=0)


class Admins:
    """Records every delivery and answers from a scripted list of results."""

    def __init__(self, *results):
        self.results = list(results)
        self.messages: list[tuple[str, str]] = []

    async def __call__(self, message: str, level: str = "info") -> AdminDeliveryResult:
        self.messages.append((message, level))
        result = self.results.pop(0) if self.results else DELIVERED
        if isinstance(result, Exception):
            raise result
        return result


def _expiring(now: datetime, hours: float = 5) -> ExecutorProfileObservation:
    return ExecutorProfileObservation(
        condition=ExecutorProfileCondition.REFRESH_EXPIRING,
        login_state=ProfileLoginState.LOGGED_IN,
        refresh_material=RefreshMaterialState.PRESENT,
        refresh_expires_at=now + timedelta(hours=hours),
        refresh_expiry_source=CredentialExpirySource.CODEX_REFRESH_TOKEN_JWT_EXP,
    )


def _snapshot(now: datetime, *, claude=None, codex=None) -> ExecutorDiagnosticSnapshot:
    expires_at = now + timedelta(seconds=90)

    def item(executor, profile):
        if profile is None:
            return ExecutorDiagnostic(
                executor=executor,
                enabled=False,
                auth_mode=ExecutorAuthMode.HOST_SESSION,
                availability="unavailable",
                observed_at=now,
                expires_at=expires_at,
                active_lease_count=0,
                reason_code="disabled",
                reason=safe_executor_diagnostic_reason("disabled"),
            )
        reason_code, availability = PROFILE_CONDITION_OUTCOMES[profile.condition]
        return ExecutorDiagnostic(
            executor=executor,
            enabled=True,
            auth_mode=ExecutorAuthMode.HOST_SESSION,
            availability=availability,
            observed_at=now,
            expires_at=expires_at,
            active_lease_count=0,
            reason_code=reason_code,
            reason=safe_executor_diagnostic_reason(reason_code),
            profile=profile,
        )

    return ExecutorDiagnosticSnapshot(
        schema_version="v2",
        version=f"snapshot-{now.timestamp()}",
        observed_at=now,
        expires_at=expires_at,
        diagnostics=[item(AgentType.CLAUDE, claude), item(AgentType.CODEX, codex)],
    )


async def _episode(redis, executor: AgentType) -> ExecutorProfileAlertEpisode | None:
    raw = await redis.get(executor_profile_alert_key(executor))
    return None if raw is None else ExecutorProfileAlertEpisode.model_validate_json(raw)


@pytest.fixture
async def redis():
    client = aioredis.FakeRedis(decode_responses=True)
    yield client
    await client.aclose()


LOGGED_OUT = host_profile(ExecutorProfileCondition.LOGGED_OUT)
HEALTHY = host_profile(ExecutorProfileCondition.HEALTHY)


@pytest.mark.asyncio
async def test_first_alertable_observation_delivers_immediately_and_settles(redis):
    admins = Admins(DELIVERED)
    alerts = ExecutorProfileAlerts(redis, admins)

    await alerts.reconcile(_snapshot(T0, claude=LOGGED_OUT, codex=HEALTHY))

    assert admins.messages == [
        (
            "Claude executor host-session profile needs attention: "
            "Host-session profile is logged out.",
            "error",
        )
    ]
    episode = await _episode(redis, AgentType.CLAUDE)
    assert episode.state is ExecutorProfileAlertState.SETTLED
    assert episode.last_outcome is ExecutorProfileAlertOutcome.DELIVERED
    assert episode.attempts == 1
    assert await _episode(redis, AgentType.CODEX) is None


@pytest.mark.asyncio
async def test_identical_snapshots_and_a_publisher_restart_do_not_notify_again(redis):
    admins = Admins()
    await ExecutorProfileAlerts(redis, admins).reconcile(_snapshot(T0, codex=LOGGED_OUT))
    first = await _episode(redis, AgentType.CODEX)

    for tick in range(1, 20):
        await ExecutorProfileAlerts(redis, admins).reconcile(
            _snapshot(T0 + timedelta(seconds=30 * tick), codex=LOGGED_OUT)
        )

    assert len(admins.messages) == 1
    assert (await _episode(redis, AgentType.CODEX)).episode_id == first.episode_id


@pytest.mark.asyncio
async def test_concurrent_ticks_open_one_episode_and_send_once(redis):
    release = asyncio.Event()
    sent: list[str] = []

    async def slow_admins(message: str, level: str = "info") -> AdminDeliveryResult:
        sent.append(message)
        await release.wait()
        return DELIVERED

    alerts = [ExecutorProfileAlerts(redis, slow_admins) for _ in range(3)]
    ticks = [
        asyncio.create_task(alert.reconcile(_snapshot(T0, codex=LOGGED_OUT))) for alert in alerts
    ]
    await asyncio.sleep(0.05)
    release.set()
    await asyncio.gather(*ticks)
    await ExecutorProfileAlerts(redis, slow_admins).reconcile(
        _snapshot(T0 + timedelta(seconds=30), codex=LOGGED_OUT)
    )

    assert len(sent) == 1
    assert (await _episode(redis, AgentType.CODEX)).state is ExecutorProfileAlertState.SETTLED


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("result", "outcome"),
    [
        (PARTIAL, ExecutorProfileAlertOutcome.PARTIAL),
        (FAILED, ExecutorProfileAlertOutcome.FAILED),
        (UNADDRESSABLE, ExecutorProfileAlertOutcome.UNADDRESSABLE),
        (RuntimeError("users API returned HTTP 503"), ExecutorProfileAlertOutcome.FAILED),
    ],
)
async def test_unsettled_delivery_stays_owed_and_retries_with_bounded_backoff(
    redis, result, outcome
):
    admins = Admins(result, result, DELIVERED)
    alerts = ExecutorProfileAlerts(redis, admins)

    await alerts.reconcile(_snapshot(T0, codex=LOGGED_OUT))
    owed = await _episode(redis, AgentType.CODEX)
    assert owed.state is ExecutorProfileAlertState.OWED
    assert owed.last_outcome is outcome
    assert owed.next_attempt_at == T0 + timedelta(seconds=60)

    # Every 30-second tick inside the backoff window is silent.
    await alerts.reconcile(_snapshot(T0 + timedelta(seconds=30), codex=LOGGED_OUT))
    assert len(admins.messages) == 1

    await alerts.reconcile(_snapshot(T0 + timedelta(seconds=60), codex=LOGGED_OUT))
    retried = await _episode(redis, AgentType.CODEX)
    assert len(admins.messages) == 2
    assert retried.episode_id == owed.episode_id
    assert retried.attempts == 2
    assert retried.next_attempt_at == T0 + timedelta(seconds=180)

    await alerts.reconcile(_snapshot(T0 + timedelta(seconds=150), codex=LOGGED_OUT))
    assert len(admins.messages) == 2
    await alerts.reconcile(_snapshot(T0 + timedelta(seconds=180), codex=LOGGED_OUT))
    settled = await _episode(redis, AgentType.CODEX)
    assert len(admins.messages) == 3
    assert settled.state is ExecutorProfileAlertState.SETTLED
    assert settled.next_attempt_at is None


def test_retry_backoff_is_bounded():
    assert [alert_retry_delay(n).total_seconds() for n in (1, 2, 3, 4)] == [60, 120, 240, 480]
    assert alert_retry_delay(50) == ALERT_RETRY_MAX


@pytest.mark.asyncio
async def test_healthy_observation_resolves_only_that_executor_and_a_regression_alerts_again(
    redis,
):
    admins = Admins()
    alerts = ExecutorProfileAlerts(redis, admins)
    await alerts.reconcile(_snapshot(T0, claude=LOGGED_OUT, codex=LOGGED_OUT))
    claude_episode = await _episode(redis, AgentType.CLAUDE)

    await alerts.reconcile(_snapshot(T0 + timedelta(seconds=30), claude=LOGGED_OUT, codex=HEALTHY))

    assert await _episode(redis, AgentType.CODEX) is None
    assert (await _episode(redis, AgentType.CLAUDE)).episode_id == claude_episode.episode_id
    assert len(admins.messages) == 2

    await alerts.reconcile(
        _snapshot(T0 + timedelta(seconds=60), claude=LOGGED_OUT, codex=LOGGED_OUT)
    )

    assert len(admins.messages) == 3
    assert admins.messages[-1][0].startswith("Codex executor")


@pytest.mark.asyncio
async def test_near_expiry_alert_names_the_expiry_and_expiry_keeps_the_same_episode(redis):
    admins = Admins()
    alerts = ExecutorProfileAlerts(redis, admins)
    expiring = _expiring(T0, hours=5)

    await alerts.reconcile(_snapshot(T0, codex=expiring))
    await alerts.reconcile(_snapshot(T0 + timedelta(seconds=30), codex=expiring))

    assert admins.messages == [
        (
            "Codex executor host-session profile needs attention: Host-session refresh "
            "credential expires within 24 hours. Refresh credential expiry: 2026-09-14T17:00:00Z.",
            "warning",
        )
    ]
    opened = await _episode(redis, AgentType.CODEX)

    expired = ExecutorProfileObservation(
        condition=ExecutorProfileCondition.REFRESH_EXPIRED,
        login_state=ProfileLoginState.EXPIRED,
        refresh_material=RefreshMaterialState.PRESENT,
        refresh_expires_at=expiring.refresh_expires_at,
        refresh_expiry_source=CredentialExpirySource.CODEX_REFRESH_TOKEN_JWT_EXP,
    )
    await alerts.reconcile(_snapshot(T0 + timedelta(hours=6), codex=expired))

    # The same unhealthy stretch: current facts change, delivery does not reopen.
    assert len(admins.messages) == 1
    current = await _episode(redis, AgentType.CODEX)
    assert current.episode_id == opened.episode_id
    assert current.condition is ExecutorProfileCondition.REFRESH_EXPIRED
    assert current.state is ExecutorProfileAlertState.SETTLED
    assert current.attempts == 1


@pytest.mark.asyncio
async def test_moves_among_unusable_states_never_reopen_a_settled_episode(redis):
    admins = Admins()
    alerts = ExecutorProfileAlerts(redis, admins)
    stretch = [
        ExecutorProfileCondition.LOGGED_OUT,
        ExecutorProfileCondition.REFRESH_MISSING,
        ExecutorProfileCondition.UNUSABLE,
        ExecutorProfileCondition.UNVERIFIABLE,
        ExecutorProfileCondition.LOGGED_OUT,
    ]

    for tick, condition in enumerate(stretch):
        await alerts.reconcile(
            _snapshot(T0 + timedelta(seconds=30 * tick), codex=host_profile(condition))
        )

    assert len(admins.messages) == 1
    episode = await _episode(redis, AgentType.CODEX)
    assert episode.condition is ExecutorProfileCondition.LOGGED_OUT
    assert episode.state is ExecutorProfileAlertState.SETTLED


@pytest.mark.asyncio
async def test_a_condition_change_keeps_owed_delivery_under_the_same_backoff(redis):
    admins = Admins(FAILED, DELIVERED)
    alerts = ExecutorProfileAlerts(redis, admins)

    await alerts.reconcile(_snapshot(T0, codex=LOGGED_OUT))
    owed = await _episode(redis, AgentType.CODEX)
    await alerts.reconcile(
        _snapshot(
            T0 + timedelta(seconds=30),
            codex=host_profile(ExecutorProfileCondition.REFRESH_MISSING),
        )
    )

    changed = await _episode(redis, AgentType.CODEX)
    assert len(admins.messages) == 1
    assert changed.episode_id == owed.episode_id
    assert changed.state is ExecutorProfileAlertState.OWED
    assert changed.next_attempt_at == owed.next_attempt_at == T0 + timedelta(seconds=60)

    await alerts.reconcile(
        _snapshot(
            T0 + timedelta(seconds=60),
            codex=host_profile(ExecutorProfileCondition.REFRESH_MISSING),
        )
    )

    assert len(admins.messages) == 2
    assert admins.messages[-1][0].endswith("Host-session profile has no refresh credential.")
    assert (await _episode(redis, AgentType.CODEX)).state is ExecutorProfileAlertState.SETTLED


@pytest.mark.asyncio
async def test_a_contended_read_neither_opens_nor_resolves_an_episode(redis):
    admins = Admins()
    alerts = ExecutorProfileAlerts(redis, admins)
    contended = host_profile(ExecutorProfileCondition.READ_CONTENDED)

    await alerts.reconcile(_snapshot(T0, codex=contended))
    assert await _episode(redis, AgentType.CODEX) is None

    await alerts.reconcile(_snapshot(T0 + timedelta(seconds=30), codex=LOGGED_OUT))
    await alerts.reconcile(_snapshot(T0 + timedelta(seconds=60), codex=contended))

    assert len(admins.messages) == 1
    assert (await _episode(redis, AgentType.CODEX)).condition is ExecutorProfileCondition.LOGGED_OUT


@pytest.mark.asyncio
async def test_unobserved_executor_is_left_alone(redis):
    admins = Admins()
    alerts = ExecutorProfileAlerts(redis, admins)
    await alerts.reconcile(_snapshot(T0, codex=LOGGED_OUT))

    await alerts.reconcile(_snapshot(T0 + timedelta(seconds=30), codex=None))

    assert await _episode(redis, AgentType.CODEX) is not None
    assert len(admins.messages) == 1


@pytest.mark.asyncio
async def test_redis_failure_never_raises_into_the_publisher():
    class BrokenRedis:
        async def set(self, *args, **kwargs):
            raise ConnectionError("redis down")

    admins = Admins()

    await ExecutorProfileAlerts(BrokenRedis(), admins).reconcile(_snapshot(T0, codex=LOGGED_OUT))

    assert admins.messages == []


@pytest.mark.asyncio
async def test_lost_or_corrupt_episode_may_repeat_an_alert_but_is_rebuilt(redis):
    admins = Admins()
    alerts = ExecutorProfileAlerts(redis, admins)
    await alerts.reconcile(_snapshot(T0, codex=LOGGED_OUT))
    await redis.set(executor_profile_alert_key(AgentType.CODEX), "{corrupt")

    await alerts.reconcile(_snapshot(T0 + timedelta(seconds=30), codex=LOGGED_OUT))

    assert len(admins.messages) == 2
    assert (await _episode(redis, AgentType.CODEX)).state is ExecutorProfileAlertState.SETTLED


@pytest.mark.asyncio
async def test_alert_and_episode_record_carry_no_credential_or_path(redis):
    admins = Admins(PARTIAL)
    alerts = ExecutorProfileAlerts(redis, admins)

    await alerts.reconcile(_snapshot(T0, codex=_expiring(T0)))

    raw = await redis.get(executor_profile_alert_key(AgentType.CODEX))
    record = json.loads(raw)
    assert set(record) == {
        "executor",
        "episode_id",
        "condition",
        "refresh_expires_at",
        "opened_at",
        "state",
        "attempts",
        "last_attempt_at",
        "last_outcome",
        "next_attempt_at",
    }
    text = raw + admins.messages[0][0]
    for fragment in ("token", "/host", ".codex", ".claude", "auth.json", "eyJ"):
        assert fragment not in text


def test_alert_message_is_executor_reason_and_expiry_only():
    episode = ExecutorProfileAlertEpisode(
        executor=AgentType.CLAUDE,
        episode_id="episode",
        condition=ExecutorProfileCondition.REFRESH_MISSING,
        opened_at=T0,
        state=ExecutorProfileAlertState.OWED,
        attempts=0,
        next_attempt_at=T0,
    )

    assert alert_message(episode) == (
        "Claude executor host-session profile needs attention: "
        "Host-session profile has no refresh credential."
    )


@pytest.mark.parametrize(
    "fields",
    [
        {"condition": "healthy", "state": "owed", "attempts": 0, "next_attempt_at": T0},
        {"condition": "read_contended", "state": "owed", "attempts": 0, "next_attempt_at": T0},
        {"condition": "logged_out", "state": "settled", "attempts": 1, "last_outcome": "partial"},
        {"condition": "logged_out", "state": "owed", "attempts": 0},
        {
            "condition": "logged_out",
            "state": "owed",
            "attempts": 1,
            "last_outcome": "delivered",
            "next_attempt_at": T0,
        },
    ],
)
def test_episode_contract_rejects_inconsistent_delivery_records(fields):
    from pydantic import ValidationError

    base = {"executor": "codex", "episode_id": "e", "opened_at": T0}
    if fields.get("attempts"):
        base["last_attempt_at"] = T0
        base.setdefault("last_outcome", fields.get("last_outcome", "failed"))
    with pytest.raises(ValidationError):
        ExecutorProfileAlertEpisode.model_validate({**base, **fields})
