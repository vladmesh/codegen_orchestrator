"""The internal API transport is written once and sends its headers itself.

Five services carried a near-identical `_get_client` / `_api_path` / `_request`,
and the copies drifted: `telegram_bot` sent no `X-Internal-Key`, the PO tools
went straight to `httpx`. Both tests below fail when either regression comes
back — one by reading the service tree, one by driving the transport.
"""

from __future__ import annotations

import ast
from pathlib import Path

import httpx
import pytest

from shared.clients.internal_api import InternalAPIClient
from shared.log_config.correlation import clear_context, set_correlation_id

REPO_ROOT = Path(__file__).parents[3]
SERVICES = REPO_ROOT / "services"

# Transport members that used to be copied per service. They live in
# shared/clients/internal_api.py now, and nowhere else.
COPIED_TRANSPORT_NAMES = frozenset({"_request", "_api_path", "_get_client"})

# A module that names one of these is talking to the internal API, so an
# `httpx.AsyncClient` built in it is a second transport rather than a call to
# some external service.
INTERNAL_API_MARKERS = ("api_base_url", "API_BASE_URL", "WORKER_API_URL", "worker_urls")

CLIENT_CLASSES = {
    "services/scheduler/src/clients/api.py": "SchedulerAPIClient",
    "services/langgraph/src/clients/api.py": "LanggraphAPIClient",
    "services/infra-service/src/clients/api.py": "InfrastructureAPIClient",
    "services/scaffolder/src/clients/api.py": "ScaffolderAPIClient",
    "services/telegram_bot/src/clients/api.py": "TelegramAPIClient",
}


def _service_sources() -> list[Path]:
    return sorted(p for p in SERVICES.glob("*/src/**/*.py") if p.is_file())


def _relative(path: Path) -> str:
    return str(path.relative_to(REPO_ROOT))


def test_no_service_defines_its_own_internal_api_transport():
    offenders = []
    for path in _service_sources():
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
                and node.name in COPIED_TRANSPORT_NAMES
            ):
                offenders.append(f"{_relative(path)}:{node.lineno} defines {node.name}")

    assert not offenders, (
        "the internal API transport is shared/clients/internal_api.py; these are copies of it:\n"
        + "\n".join(offenders)
    )


def test_no_service_builds_its_own_httpx_client_for_the_internal_api():
    offenders = []
    for path in _service_sources():
        source = path.read_text()
        if not any(marker in source for marker in INTERNAL_API_MARKERS):
            continue
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr == "AsyncClient":
                offenders.append(f"{_relative(path)}:{node.lineno} builds httpx.AsyncClient")

    assert not offenders, (
        "calls to the internal API go through InternalAPIClient, which puts "
        "X-Internal-Key and X-Correlation-ID on every one of them:\n" + "\n".join(offenders)
    )


def test_po_tools_hold_no_httpx_client():
    """The PO tools used to keep a module-level `httpx.AsyncClient` of their own."""
    po_dir = SERVICES / "langgraph" / "src" / "agents" / "po"
    offenders = [
        _relative(path) for path in sorted(po_dir.glob("*.py")) if "httpx" in path.read_text()
    ]
    assert not offenders, f"PO tools reach the internal API through the shared client: {offenders}"


@pytest.mark.parametrize(("module", "class_name"), sorted(CLIENT_CLASSES.items()))
def test_service_clients_take_their_transport_from_shared(module: str, class_name: str):
    """Each service keeps its own application methods and its public class name."""
    tree = ast.parse((REPO_ROOT / module).read_text())
    classes = {node.name: node for node in ast.walk(tree) if isinstance(node, ast.ClassDef)}
    assert class_name in classes, f"{module} no longer defines {class_name}"
    bases = [base.id for base in classes[class_name].bases if isinstance(base, ast.Name)]
    assert bases == ["InternalAPIClient"], (
        f"{class_name} must take its transport from InternalAPIClient, got bases {bases}"
    )


# ---------------------------------------------------------------------------
# The headers are a property of the transport, not of the callers
# ---------------------------------------------------------------------------

INTERNAL_KEY = "transport-test-key"


class _Recorder:
    """Captures the requests a client actually put on the wire."""

    def __init__(self, status_code: int = 200) -> None:
        self.requests: list[httpx.Request] = []
        self.status_code = status_code

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(self.status_code, json={"ok": True})

    @property
    def last(self) -> httpx.Request:
        assert self.requests, "no request reached the transport"
        return self.requests[-1]


@pytest.fixture
def recorder(monkeypatch) -> _Recorder:
    """Route every client built in this module through a recording transport."""
    rec = _Recorder()
    real_async_client = httpx.AsyncClient

    def factory(**kwargs):
        return real_async_client(transport=httpx.MockTransport(rec), **kwargs)

    monkeypatch.setattr("shared.clients.internal_api.httpx.AsyncClient", factory)
    monkeypatch.setenv("INTERNAL_API_KEY", INTERNAL_KEY)
    return rec


@pytest.fixture(autouse=True)
def _clean_correlation_context():
    clear_context()
    yield
    clear_context()


@pytest.fixture
def client(recorder) -> InternalAPIClient:
    return InternalAPIClient("http://api:8000")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "call",
    [
        lambda c: c.request("GET", "projects/"),
        lambda c: c.request_raw("POST", "projects/", json={}),
        lambda c: c.get_raw("projects/"),
        lambda c: c.post_raw("projects/", json={}),
        lambda c: c.patch_raw("projects/1", json={}),
    ],
    ids=["request", "request_raw", "get_raw", "post_raw", "patch_raw"],
)
async def test_every_call_carries_the_internal_key(client, recorder, call):
    await call(client)
    assert recorder.last.headers["X-Internal-Key"] == INTERNAL_KEY


@pytest.mark.asyncio
async def test_every_call_carries_the_bound_correlation_id(client, recorder):
    set_correlation_id("corr-42")
    await client.request("GET", "projects/")
    assert recorder.last.headers["X-Correlation-ID"] == "corr-42"


@pytest.mark.asyncio
async def test_caller_headers_cannot_drop_the_internal_key(client, recorder):
    set_correlation_id("corr-42")
    await client.request("GET", "projects/", headers={"X-Telegram-ID": "7", "X-Internal-Key": "no"})
    sent = recorder.last.headers
    assert sent["X-Internal-Key"] == INTERNAL_KEY
    assert sent["X-Correlation-ID"] == "corr-42"
    assert sent["X-Telegram-ID"] == "7"


@pytest.mark.asyncio
async def test_no_correlation_id_bound_means_no_header(client, recorder):
    await client.request("GET", "projects/")
    assert "X-Correlation-ID" not in recorder.last.headers


@pytest.mark.asyncio
async def test_paths_are_prefixed_once(client, recorder):
    await client.request("GET", "/projects/1")
    assert recorder.last.url.path == "/api/projects/1"


@pytest.mark.asyncio
async def test_a_path_that_already_says_api_is_refused(client):
    with pytest.raises(ValueError, match="/api prefix"):
        await client.request("GET", "api/projects/1")


def test_a_base_url_that_already_says_api_is_refused(recorder):
    with pytest.raises(RuntimeError, match="must not include /api"):
        InternalAPIClient("http://api:8000/api")


@pytest.mark.asyncio
async def test_request_raises_on_an_error_status_and_request_raw_does_not(recorder, monkeypatch):
    recorder.status_code = 500
    client = InternalAPIClient("http://api:8000")

    resp = await client.request_raw("GET", "projects/")
    assert resp.status_code == 500

    with pytest.raises(httpx.HTTPStatusError):
        await client.request("GET", "projects/")


@pytest.mark.asyncio
async def test_the_timeout_is_a_parameter_not_a_second_transport(recorder):
    default = InternalAPIClient("http://api:8000")
    impatient = InternalAPIClient("http://api:8000", timeout=10.0)

    assert (await default._get_client()).timeout.read == 30.0
    assert (await impatient._get_client()).timeout.read == 10.0
