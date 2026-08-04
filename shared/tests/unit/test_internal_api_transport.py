"""The internal API transport is written once and sends its headers itself.

Five services carried a near-identical `_get_client` / `_api_path` / `_request`,
and the copies drifted: `telegram_bot` sent no `X-Internal-Key`, the PO tools
went straight to `httpx`. The static guards below read the whole tree — services
and `shared/` — because reading only `services/` is how the raw `httpx` in
`shared/config_store.py` and the raw `aiohttp` in `shared/notifications.py`
survived the round that was supposed to close this. The driving tests check the
other half: both headers leave on every call.
"""

from __future__ import annotations

import ast
from pathlib import Path

import httpx
import pytest

from shared.clients.internal_api import InternalAPIClient, InternalAPISyncClient
from shared.log_config.correlation import clear_context, get_correlation_id, set_correlation_id

REPO_ROOT = Path(__file__).parents[3]
SERVICES = REPO_ROOT / "services"
SHARED = REPO_ROOT / "shared"

# Transport members that used to be copied per service. They live in
# shared/clients/internal_api.py now, and nowhere else.
COPIED_TRANSPORT_NAMES = frozenset({"_request", "_api_path", "_get_client"})

# A module that names one of these is talking to the internal API, so an
# `httpx` client built in it is a second transport rather than a call to some
# external service.
INTERNAL_API_MARKERS = (
    "api_base_url",
    "API_BASE_URL",
    "WORKER_API_URL",
    "worker_urls",
    "api_url",
)

# The client classes of every HTTP library in the tree. A module that builds one
# of these aimed at the internal API is a second transport. Which library a name
# came from is resolved from the module's imports, so `import httpx as h`,
# `from httpx import AsyncClient` and `from aiohttp import ClientSession as S`
# are the same finding as `httpx.AsyncClient` — the rule is about what the code
# does, not about how it spelled its import.
HTTP_CLIENT_CLASSES = {
    "httpx": frozenset({"AsyncClient", "Client"}),
    "aiohttp": frozenset({"ClientSession"}),
}

# Anything that puts a request on the wire. Flagged when its URL argument comes
# from the internal API base URL, whatever library it belongs to.
HTTP_VERBS = frozenset(
    {"get", "post", "put", "patch", "delete", "head", "options", "request", "stream"}
)

# The one module allowed to build a client for the internal API.
TRANSPORT_MODULE = SHARED / "clients" / "internal_api.py"

# The live-stand cleanup path sends only X-Internal-Key. It reaches a live host,
# which card 1144 left out of scope; the exemption is recorded in the sprint
# entry and is to be filed as an issue at closing, and this is the only entry
# here.
DEFERRED_OFFENDERS = (SHARED / "live_harness_cleanup.py",)

CLIENT_CLASSES = {
    "services/scheduler/src/clients/api.py": "SchedulerAPIClient",
    "services/langgraph/src/clients/api.py": "LanggraphAPIClient",
    "services/infra-service/src/clients/api.py": "InfrastructureAPIClient",
    "services/scaffolder/src/clients/api.py": "ScaffolderAPIClient",
    "services/telegram_bot/src/clients/api.py": "TelegramAPIClient",
}


def _guarded_sources() -> list[Path]:
    """Every module the rule applies to: service code and the shared tree."""
    service_sources = SERVICES.glob("*/src/**/*.py")
    shared_sources = (p for p in SHARED.glob("**/*.py") if "tests" not in p.parts)
    candidates = {*service_sources, *shared_sources} - {TRANSPORT_MODULE, *DEFERRED_OFFENDERS}
    return sorted(p for p in candidates if p.is_file())


def _relative(path: Path) -> str:
    return str(path.relative_to(REPO_ROOT))


def _names_a_marker(text: str) -> bool:
    return any(marker in text for marker in INTERNAL_API_MARKERS)


def _internal_api_url_names(tree: ast.AST) -> set[str]:
    """Names holding a URL built from the internal API base URL.

    `url = f"{config['api_url']}/api/users"` then `session.get(url)` is the same
    bypass as inlining the f-string, so the name carries the taint.
    """
    tainted: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign | ast.AnnAssign) and node.value is not None:
            if not _names_a_marker(ast.unparse(node.value)):
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                for name in ast.walk(target):
                    if isinstance(name, ast.Name):
                        tainted.add(name.id)
    return tainted


def _http_library_imports(tree: ast.AST) -> tuple[dict[str, str], dict[str, str]]:
    """What the module's own names refer to, for the HTTP libraries it imported.

    Returns the local names bound to a library module (`import httpx as h` gives
    `h -> httpx`) and the local names bound to one of its client classes
    (`from aiohttp import ClientSession as S` gives `S -> aiohttp.ClientSession`).
    """
    module_aliases: dict[str, str] = {}
    class_aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in HTTP_CLIENT_CLASSES:
                    module_aliases[alias.asname or alias.name] = alias.name
        elif isinstance(node, ast.ImportFrom) and node.module in HTTP_CLIENT_CLASSES:
            for alias in node.names:
                if alias.name in HTTP_CLIENT_CLASSES[node.module]:
                    class_aliases[alias.asname or alias.name] = f"{node.module}.{alias.name}"
    return module_aliases, class_aliases


def _client_class_built(call: ast.Call, module_aliases: dict, class_aliases: dict) -> str | None:
    """The HTTP client class this call constructs, whatever name it was given."""
    func = call.func
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
        library = module_aliases.get(func.value.id)
        if library and func.attr in HTTP_CLIENT_CLASSES[library]:
            return f"{library}.{func.attr}"
        return None
    if isinstance(func, ast.Name):
        return class_aliases.get(func.id)
    return None


def _argument_names_the_internal_api(call: ast.Call, tainted: set[str]) -> bool:
    for argument in [*call.args, *(kw.value for kw in call.keywords)]:
        if _names_a_marker(ast.unparse(argument)):
            return True
        if isinstance(argument, ast.Name) and argument.id in tainted:
            return True
    return False


def _sends_an_internal_api_path(tree: ast.AST) -> bool:
    """Whether some request in the module asks for a path of the internal API.

    A client built without a target takes one per request, so `/api/...` on a
    request is what says the client it was built from is aimed at the internal
    API rather than at some external service.
    """
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr not in HTTP_VERBS:
            continue
        for argument in [*node.args, *(kw.value for kw in node.keywords)]:
            if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
                if argument.value.lstrip("/").startswith("api/"):
                    return True
    return False


def find_raw_internal_api_calls(source: str, label: str) -> list[str]:
    """Report every raw HTTP call to the internal API in one module.

    Two shapes count: a module that builds its own HTTP client aimed at the
    internal API, and any request whose URL argument comes from the internal API
    base URL — including through a local variable, and including libraries other
    than httpx. Both are read off what the code does: the client class is
    resolved through the module's imports, so no spelling of the import hides
    the first shape.
    """
    tree = ast.parse(source)
    module_talks_to_internal_api = _names_a_marker(source)
    tainted = _internal_api_url_names(tree)
    module_aliases, class_aliases = _http_library_imports(tree)
    asks_for_internal_paths = module_talks_to_internal_api and _sends_an_internal_api_path(tree)
    offenders = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue

        built = _client_class_built(node, module_aliases, class_aliases)
        if built is not None:
            if _argument_names_the_internal_api(node, tainted) or asks_for_internal_paths:
                offenders.append(f"{label}:{node.lineno} builds {built}")
            continue

        func = node.func
        if not isinstance(func, ast.Attribute) or func.attr not in HTTP_VERBS:
            continue
        arguments = [*node.args, *(kw.value for kw in node.keywords)]
        for argument in arguments:
            text = ast.unparse(argument)
            if _names_a_marker(text) or (isinstance(argument, ast.Name) and argument.id in tainted):
                offenders.append(f"{label}:{node.lineno} sends {ast.unparse(func)}({text}) itself")
                break

    return offenders


def _transport_copy_candidates() -> list[Path]:
    """Service code, plus the shared modules that name the internal API.

    `shared/clients/` also holds clients for external services, and their own
    `_request` is not a copy of this transport, so outside `services/` the rule
    applies to the modules that reach the internal API.
    """
    return [
        path
        for path in _guarded_sources()
        if SERVICES in path.parents or _names_a_marker(path.read_text())
    ]


def test_no_module_defines_its_own_internal_api_transport():
    offenders = []
    for path in _transport_copy_candidates():
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


def test_no_module_reaches_the_internal_api_without_the_shared_transport():
    offenders = []
    for path in _guarded_sources():
        offenders += find_raw_internal_api_calls(path.read_text(), _relative(path))

    assert not offenders, (
        "calls to the internal API go through the shared transport, which puts "
        "X-Internal-Key and X-Correlation-ID on every one of them:\n" + "\n".join(offenders)
    )


def test_the_guard_is_a_rule_and_not_a_list_of_known_files(tmp_path):
    """A module that does not exist yet has to fail it too."""
    newcomer = (
        "import httpx\n"
        "\n"
        "async def read_projects(api_base_url: str):\n"
        "    async with httpx.AsyncClient(base_url=api_base_url) as client:\n"
        "        return await client.get('/api/projects/')\n"
    )
    assert find_raw_internal_api_calls(newcomer, "shared/newcomer.py")

    with_aiohttp = (
        "import aiohttp\n"
        "\n"
        "async def read_users(api_url: str):\n"
        "    url = f'{api_url}/api/users'\n"
        "    async with aiohttp.ClientSession() as session:\n"
        "        async with session.get(url) as resp:\n"
        "            return await resp.json()\n"
    )
    assert find_raw_internal_api_calls(with_aiohttp, "shared/newcomer.py")

    external = (
        "import httpx\n"
        "\n"
        "async def read_billing(token: str):\n"
        "    async with httpx.AsyncClient(base_url='https://billing.example.com') as client:\n"
        "        return await client.get('/api/v1/orders', headers={'X-Token': token})\n"
    )
    assert not find_raw_internal_api_calls(external, "shared/clients/billing.py")


@pytest.mark.parametrize(
    ("form", "module"),
    [
        (
            "from-import",
            "from httpx import AsyncClient\n"
            "\n"
            "async def read_users(api_base_url: str):\n"
            "    async with AsyncClient(base_url=api_base_url) as client:\n"
            "        return await client.get('/api/users')\n",
        ),
        (
            "from-import aliased",
            "from httpx import Client as HTTP\n"
            "\n"
            "def read_users(api_base_url: str):\n"
            "    with HTTP(base_url=api_base_url) as client:\n"
            "        return client.get('/api/users')\n",
        ),
        (
            "module alias",
            "import httpx as h\n"
            "\n"
            "async def read_users(api_base_url: str):\n"
            "    async with h.AsyncClient(base_url=api_base_url) as client:\n"
            "        return await client.get('/api/users')\n",
        ),
        (
            "aiohttp session with a base url",
            "from aiohttp import ClientSession\n"
            "\n"
            "async def read_users(api_url: str):\n"
            "    async with ClientSession(base_url=api_url) as session:\n"
            "        async with session.get('/api/users') as resp:\n"
            "            return await resp.json()\n",
        ),
        (
            "aiohttp module alias",
            "import aiohttp as web\n"
            "\n"
            "async def read_users(api_url: str):\n"
            "    async with web.ClientSession(base_url=api_url) as session:\n"
            "        async with session.get('/api/users') as resp:\n"
            "            return await resp.json()\n",
        ),
        (
            "base url through a local name",
            "import httpx\n"
            "import os\n"
            "\n"
            "async def read_users():\n"
            "    base = os.environ['API_BASE_URL']\n"
            "    async with httpx.AsyncClient(base_url=base) as client:\n"
            "        return await client.get('/api/users')\n",
        ),
    ],
)
def test_the_guard_does_not_depend_on_the_shape_of_the_import(form: str, module: str):
    """Importing the client class by name is the same bypass as reaching for it.

    The guard used to match `<something>.AsyncClient`, so `from httpx import
    AsyncClient` walked past it, and `aiohttp` was only ever caught through the
    URL of a request. What the module does is the rule; how it spelled the import
    is not part of it.
    """
    assert find_raw_internal_api_calls(module, "shared/newcomer.py"), form


def test_a_session_aimed_at_another_service_is_not_a_finding():
    """`shared/notifications.py`: reads the internal API, posts to Telegram."""
    telegram = (
        "import aiohttp\n"
        "\n"
        "from shared.clients.internal_api import InternalAPIClient\n"
        "\n"
        "async def notify(api_url: str, token: str, text: str):\n"
        "    client = InternalAPIClient(api_url)\n"
        "    users = await client.get_raw('users')\n"
        "    async with aiohttp.ClientSession() as session:\n"
        "        url = f'https://api.telegram.org/bot{token}/sendMessage'\n"
        "        await session.post(url, json={'text': text})\n"
        "    return users\n"
    )
    assert not find_raw_internal_api_calls(telegram, "shared/notifications.py")


def test_the_guard_reads_both_trees():
    scanned = {_relative(path) for path in _guarded_sources()}
    assert any(name.startswith("services/") for name in scanned)
    assert any(name.startswith("shared/") for name in scanned)


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
    real_sync_client = httpx.Client

    def async_factory(**kwargs):
        return real_async_client(transport=httpx.MockTransport(rec), **kwargs)

    def sync_factory(**kwargs):
        return real_sync_client(transport=httpx.MockTransport(rec), **kwargs)

    monkeypatch.setattr("shared.clients.internal_api.httpx.AsyncClient", async_factory)
    monkeypatch.setattr("shared.clients.internal_api.httpx.Client", sync_factory)
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
async def test_every_call_carries_both_headers(client, recorder, call):
    await call(client)
    assert recorder.last.headers["X-Internal-Key"] == INTERNAL_KEY
    assert recorder.last.headers["X-Correlation-ID"]


@pytest.mark.asyncio
async def test_every_call_carries_the_bound_correlation_id(client, recorder):
    set_correlation_id("corr-42")
    await client.request("GET", "projects/")
    assert recorder.last.headers["X-Correlation-ID"] == "corr-42"


def _fields_named(request: httpx.Request, name: str) -> list[str]:
    """Every field on the wire with that name, HTTP's case-insensitive way."""
    return [
        value.decode() for key, value in request.headers.raw if key.decode().lower() == name.lower()
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "spelling",
    ["X-Internal-Key", "x-internal-key", "X-INTERNAL-KEY", "x-Internal-key"],
)
async def test_caller_headers_cannot_drop_the_internal_key(client, recorder, spelling):
    """Header names are case-insensitive, so another spelling is the same field.

    `x-internal-key: forged` used to be copied through and left ahead of the
    transport's own field: two fields with one name on the wire, and the API
    reads the first, which is how a forged key reached it and came back a 401.
    """
    set_correlation_id("corr-42")
    await client.request("GET", "projects/", headers={"X-Telegram-ID": "7", spelling: "forged"})

    sent = recorder.last
    assert _fields_named(sent, "X-Internal-Key") == [INTERNAL_KEY]
    assert _fields_named(sent, "X-Correlation-ID") == ["corr-42"]
    assert sent.headers["X-Telegram-ID"] == "7"


@pytest.mark.asyncio
@pytest.mark.parametrize("spelling", ["x-correlation-id", "X-CORRELATION-ID", "x-Correlation-Id"])
async def test_a_caller_named_id_in_any_spelling_is_sent_once_canonically(
    client, recorder, spelling
):
    """One field, canonically named, holding the identifier the flow will carry."""
    await client.request("GET", "projects/", headers={spelling: "caller-cid"})

    sent = recorder.last
    assert _fields_named(sent, "X-Correlation-ID") == ["caller-cid"]
    assert "X-Correlation-ID" in [key.decode() for key, _ in sent.headers.raw]
    assert get_correlation_id() == "caller-cid"


@pytest.mark.asyncio
async def test_an_unbound_context_gets_an_id_from_the_transport(client, recorder):
    """The bot and the scheduler loops bind nothing; their calls are labelled anyway.

    This replaces `test_no_correlation_id_bound_means_no_header`, which asserted
    that an unbound context means an unlabelled call. Card 1144 makes the header
    unconditional, so that assertion states the old contract, not a regression.
    """
    await client.request("GET", "projects/")
    generated = recorder.last.headers["X-Correlation-ID"]
    assert generated

    await client.request("GET", "projects/2")
    assert recorder.last.headers["X-Correlation-ID"] == generated
    assert get_correlation_id() == generated


@pytest.mark.asyncio
async def test_a_caller_named_id_on_an_unbound_context_becomes_the_flow_id(client, recorder):
    """What goes on the wire is what the rest of the flow will carry.

    With nothing bound and the caller naming its own `X-Correlation-ID`, the
    transport used to generate an identifier, bind that one, and send the
    caller's — so the first call of a flow was labelled differently from every
    call after it.
    """
    await client.request("GET", "projects/", headers={"X-Correlation-ID": "caller-id"})
    assert recorder.last.headers["X-Correlation-ID"] == "caller-id"
    assert get_correlation_id() == "caller-id"

    await client.request("GET", "projects/2")
    assert recorder.last.headers["X-Correlation-ID"] == "caller-id"


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


# ---------------------------------------------------------------------------
# The synchronous form sends the same two headers
# ---------------------------------------------------------------------------


def test_the_sync_client_sends_both_headers_and_prefixes_the_path(recorder):
    set_correlation_id("corr-7")
    client = InternalAPISyncClient("http://api:8000")

    client.request("GET", "system-configs/scheduler.interval")

    sent = recorder.last
    assert sent.headers["X-Internal-Key"] == INTERNAL_KEY
    assert sent.headers["X-Correlation-ID"] == "corr-7"
    assert sent.url.path == "/api/system-configs/scheduler.interval"
    client.close()


def test_the_sync_client_labels_an_unbound_context_too(recorder):
    client = InternalAPISyncClient("http://api:8000")

    client.get_raw("system-configs/")
    generated = recorder.last.headers["X-Correlation-ID"]
    assert generated

    client.get_raw("system-configs/")
    assert recorder.last.headers["X-Correlation-ID"] == generated
    client.close()
