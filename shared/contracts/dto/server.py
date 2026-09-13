from datetime import datetime
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, model_validator

from shared.contracts.dto.base import TimestampedDTO
from shared.provisioning_policy import ADMIN_SSH_USER

SSHUser = Annotated[str, Field(min_length=1, max_length=32, pattern=r"^[a-z_][a-z0-9_-]*$")]
# The exact deployed commit a reconciliation ran from.
DeployedRevision = Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]
TARGET_READINESS_DETAIL_MAX = 500


class ServerStatus(StrEnum):
    """Server status lifecycle."""

    # Discovery
    DISCOVERED = "discovered"  # Обнаружен в Time4VPS API
    NEW = "new"  # Новый, ещё не классифицирован
    PENDING_SETUP = "pending_setup"  # Новый managed сервер, требует настройки

    # Provisioning
    PROVISIONING = "provisioning"  # Идет базовая настройка
    FORCE_REBUILD = "force_rebuild"  # 🔥 ТРИГГЕР: Полная переустановка

    # Operational
    READY = "ready"  # Настроен, готов принимать сервисы
    IN_USE = "in_use"  # Имеет активные сервисы
    ACTIVE = "active"  # Доступен и работает

    # Issues
    ERROR = "error"  # Инцидент: был в норме, доступ пропал
    UNREACHABLE = "unreachable"  # Недоступен по сети

    # Archive
    RESERVED = "reserved"  # Inventory-only; no provisioning is scheduled
    MISSING = "missing"  # Пропал из Time4VPS API


class TargetReadinessPhase(StrEnum):
    """The step of managed-target reconciliation that failed, in execution order.

    Each is its own executed step: a failure — a timeout included — is the step
    that was running, never a phase read back out of another step's output.
    """

    SSH_KEY_MISSING = "ssh_key_missing"
    SSH_KEY_INVALID = "ssh_key_invalid"
    ADMIN_LOGIN = "admin_login"
    PRIVILEGE_PREFLIGHT = "privilege_preflight"
    QA_IDENTITY_ROLE = "qa_identity_role"
    QA_IDENTITY_PROOF = "qa_identity_proof"


class TargetIdentity(BaseModel):
    """The connection a readiness verdict was proved over.

    A verdict is applied only while the row still has exactly this identity, so
    a key, account or address change that lands during a reconciliation never
    receives the old identity's receipt or park. The fingerprint is `None` when
    the stored key is missing or does not parse.
    """

    model_config = ConfigDict(extra="forbid")

    ssh_user: SSHUser
    host: str
    public_ip: str
    ssh_key_fingerprint: str | None


class QATargetReceipt(BaseModel):
    """A readiness receipt a provisioning success records together with READY.

    The proof comes from the software play; the identity is the row's connection
    identity with the fingerprint of the key the success handler just persisted.
    """

    model_config = ConfigDict(extra="forbid")

    profile_version: str
    proved_at: datetime
    identity: TargetIdentity


class ServerCreate(BaseModel):
    """Create server request.

    The fields are the columns of the `servers` row, except `ssh_key`: the raw
    key is never stored, the API encrypts it into `ssh_key_enc`.
    """

    handle: str
    host: str
    public_ip: str
    ssh_user: SSHUser = ADMIN_SSH_USER
    ssh_key: str | None = Field(default=None, description="Raw SSH private key to be encrypted")
    capacity_cpu: int = 1
    capacity_ram_mb: int = 1024
    capacity_disk_mb: int = 10240
    is_managed: bool = True
    status: ServerStatus = ServerStatus.DISCOVERED
    notes: str | None = None
    labels: dict = {}


class ServerUpdate(BaseModel):
    """Update server request."""

    handle: str | None = None
    host: str | None = None
    public_ip: str | None = None
    ssh_user: SSHUser | None = None
    ssh_key: str | None = None
    status: ServerStatus | None = None
    labels: dict | None = None
    is_managed: bool | None = None
    provider: str | None = None
    provider_id: str | None = None
    capacity_cpu: int | None = None
    capacity_ram_mb: int | None = None
    capacity_disk_mb: int | None = None
    used_ram_mb: int | None = None
    used_disk_mb: int | None = None
    os_template: str | None = None
    provisioning_started_at: datetime | None = None
    # Health metrics (from node_exporter + cadvisor)
    cpu_usage_pct: float | None = None
    load_avg_1m: float | None = None
    load_avg_5m: float | None = None
    load_avg_15m: float | None = None
    network_rx_errors: int | None = None
    network_tx_errors: int | None = None
    container_count_running: int | None = None
    container_count_total: int | None = None
    uptime_seconds: float | None = None
    last_health_check: datetime | None = None


class ProvisioningAttemptReservation(BaseModel):
    """Request to reserve one provisioning attempt atomically."""

    max_attempts: int = Field(gt=0)


class ProvisioningAttemptReservationResult(BaseModel):
    """Result of reserving an attempt for the current provisioning episode."""

    reserved: bool
    provisioning_attempts: int
    episode_id: str | None = None


class ProvisioningAttemptReset(BaseModel):
    """Request to close an episode only when its attempt is still current.

    READY is never written without the readiness receipt of the provisioning that
    earned it: both are applied in one transaction, or neither is.
    """

    attempt_number: int = Field(gt=0)
    episode_id: str = Field(min_length=1)
    qa_target_receipt: QATargetReceipt


class ProvisioningAttemptResetResult(BaseModel):
    """Result of conditionally closing a provisioning attempt episode."""

    reset: bool
    provisioning_attempts: int
    episode_id: str | None = None


class ServerDTO(TimestampedDTO):
    """Server response."""

    handle: str
    host: str
    public_ip: str
    ssh_user: SSHUser
    status: ServerStatus
    provider: str | None = None  # Computed from labels
    provider_id: str | None = None  # Computed from labels
    is_managed: bool
    labels: dict = {}

    capacity_cpu: int = 0
    capacity_ram_mb: int = 0
    capacity_disk_mb: int = 0
    used_ram_mb: int = 0
    used_disk_mb: int = 0
    os_template: str | None = None

    # Health metrics (from node_exporter + cadvisor)
    cpu_usage_pct: float | None = None
    load_avg_1m: float | None = None
    load_avg_5m: float | None = None
    load_avg_15m: float | None = None
    network_rx_errors: int | None = None
    network_tx_errors: int | None = None
    container_count_running: int | None = None
    container_count_total: int | None = None
    uptime_seconds: float | None = None

    last_health_check: datetime | None = None
    provisioning_started_at: datetime | None = None
    provisioning_attempts: int = 0
    provisioning_episode_id: str | None = None

    # Public fingerprint of the stored administrative key; the key itself is
    # never part of a server response.
    ssh_key_fingerprint: str | None = None
    # The QA target readiness receipt: the profile the target last proved, and
    # when. Written only by `POST /api/servers/{handle}/target-readiness` after a
    # successful role proof — never by a label and never by PATCH.
    qa_target_version: str | None = None
    qa_target_proved_at: datetime | None = None
    # The phase of the last readiness failure while it is unrepaired. Set and
    # cleared only by the readiness endpoint; admission refuses the row while it
    # is set, whatever its status says.
    target_readiness_failure_phase: TargetReadinessPhase | None = None


def target_identity(server: ServerDTO, ssh_key_fingerprint: str | None) -> TargetIdentity:
    """The connection identity of this row, with the fingerprint of its stored key."""
    return TargetIdentity(
        ssh_user=server.ssh_user,
        host=server.host,
        public_ip=server.public_ip,
        ssh_key_fingerprint=ssh_key_fingerprint,
    )


class TargetReadinessReport(BaseModel):
    """One reconciliation verdict for one managed target.

    Exactly one shape each way: a ready target carries the profile it proved and
    when, and names no failed phase; a target that is not ready names the phase
    that failed and carries no profile, so a failure can never be read as a
    receipt.
    """

    model_config = ConfigDict(extra="forbid")

    ready: bool
    profile_version: str | None = None
    proved_at: datetime | None = None
    phase: TargetReadinessPhase | None = None
    detail: str = Field(default="", max_length=TARGET_READINESS_DETAIL_MAX)
    revision: DeployedRevision | None = None
    identity: TargetIdentity

    @model_validator(mode="after")
    def _one_verdict(self) -> "TargetReadinessReport":
        if self.ready:
            if self.profile_version is None or self.proved_at is None or self.phase is not None:
                raise ValueError("a ready target carries profile_version and proved_at, no phase")
        elif self.phase is None or self.profile_version is not None or self.proved_at is not None:
            raise ValueError("a target that is not ready carries a phase and no receipt")
        return self


class TargetReadinessRead(BaseModel):
    """What the server row says after a readiness verdict was applied."""

    server_handle: str
    ready: bool
    status: ServerStatus
    qa_target_version: str | None = None
    qa_target_proved_at: datetime | None = None
    target_readiness_failure_phase: TargetReadinessPhase | None = None
    incident_id: int | None = None


class ServerMetricsHistoryDTO(BaseModel):
    """Server metrics history entry."""

    id: int | None = None
    server_handle: str
    recorded_at: datetime
    metrics: dict
