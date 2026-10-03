"""The live kit catalog reader: one fetch, the kit's own loader, and a typed failure.

The HTTP boundary is `respx`; everything past it is real — `parse_catalog` from the
pinned kit tooling, `CatalogPackage.select` against the pinned `CORE_VERSION`, and the
reader's cache over an injected clock. Every way a read can fail answers
`KitCatalogUnavailable` and never raises, and nothing old or hard-coded stands in.
"""

from __future__ import annotations

from framework.spec.package_resolution import CORE_VERSION
import httpx
import pytest
import respx

from src.kit_catalog import (
    CATALOG_TIMEOUT_SECONDS,
    KitCatalog,
    KitCatalogFailure,
    KitCatalogReader,
    KitCatalogUnavailable,
    catalog_url,
    get_kit_catalog_reader,
)

URL = "https://raw.example.invalid/kit/HEAD/packages/catalog.yaml"

CATALOG = """\
format_version: 1
packages:
  - name: reminders
    distribution: codegen-kit-reminders
    path: packages/codegen-kit-reminders
    summary: One-time text reminders.
    capabilities: [remind me at a time]
    settings:
      - name: reminder_owner_ref
        summary: Owner of the seeded reminder.
    environment:
      - name: REDIS_URL
        required: true
        summary: Redis broker.
    versions:
      - version: 0.3.0
        tag: packages/reminders/v0.3.0
        requires_core: ">=2,<3"
      - version: 0.4.0
        tag: packages/reminders/v0.4.0
        requires_core: ">=2.1,<3"
  - name: invoices
    distribution: codegen-kit-invoices
    path: packages/codegen-kit-invoices
    summary: Invoices for a core this orchestrator does not pin.
    capabilities: [send an invoice]
    settings: []
    environment: []
    versions:
      - version: 1.0.0
        tag: packages/invoices/v1.0.0
        requires_core: ">=3,<4"
"""


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _reader(clock: _Clock | None = None, ttl: float = 300.0) -> KitCatalogReader:
    return KitCatalogReader(URL, ttl=ttl, clock=clock or _Clock())


@pytest.mark.asyncio
async def test_a_fresh_read_keeps_the_packages_the_pinned_core_can_install():
    with respx.mock(assert_all_called=True) as http:
        http.get(URL).mock(return_value=httpx.Response(200, text=CATALOG))
        answer = await _reader().read()

    assert isinstance(answer, KitCatalog)
    assert answer.source == URL
    assert answer.core_version == CORE_VERSION
    # `invoices` needs core 3: `kit add` would refuse it, so the planner never sees it.
    assert answer.names == frozenset({"reminders"})
    (reminders,) = answer.packages
    assert reminders.version.version == "0.4.0"
    assert reminders.package.capabilities == ("remind me at a time",)
    assert [setting.name for setting in reminders.package.settings] == ["reminder_owner_ref"]


@pytest.mark.asyncio
async def test_a_read_within_the_ttl_answers_without_a_fetch_and_a_later_one_fetches():
    clock = _Clock()
    reader = _reader(clock, ttl=300.0)
    with respx.mock() as http:
        route = http.get(URL).mock(return_value=httpx.Response(200, text=CATALOG))
        first = await reader.read()
        clock.now += 299.0
        second = await reader.read()
        assert route.call_count == 1
        clock.now += 1.0
        third = await reader.read()
        assert route.call_count == 2

    assert second is first
    assert isinstance(third, KitCatalog) and third.names == first.names


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response", "failure", "detail"),
    [
        pytest.param(httpx.ConnectError("refused"), KitCatalogFailure.TRANSPORT, "ConnectError"),
        pytest.param(httpx.ReadTimeout("slow"), KitCatalogFailure.TRANSPORT, "ReadTimeout"),
        pytest.param(httpx.Response(404, text="404"), KitCatalogFailure.STATUS, "HTTP 404"),
        pytest.param(httpx.Response(503), KitCatalogFailure.STATUS, "HTTP 503"),
        pytest.param(
            httpx.Response(200, text="packages: [unclosed"),
            KitCatalogFailure.INVALID,
            "is not valid YAML",
            id="yaml",
        ),
        pytest.param(
            httpx.Response(200, text=CATALOG.replace("format_version: 1", "format_version: 2")),
            KitCatalogFailure.INVALID,
            "UnsupportedCatalogFormatError",
            id="format",
        ),
        pytest.param(
            httpx.Response(
                200, text=CATALOG.replace("    summary: One-time text reminders.\n", "")
            ),
            KitCatalogFailure.INVALID,
            "needs a non-empty string 'summary'",
            id="validation",
        ),
        pytest.param(
            httpx.Response(200, text="- just\n- a list\n"),
            KitCatalogFailure.INVALID,
            "must be a mapping",
            id="not-a-mapping",
        ),
    ],
)
async def test_every_failure_answers_unavailable_and_never_raises(response, failure, detail):
    with respx.mock() as http:
        route = http.get(URL)
        if isinstance(response, Exception):
            route.mock(side_effect=response)
        else:
            route.mock(return_value=response)
        answer = await _reader().read()

    assert isinstance(answer, KitCatalogUnavailable)
    assert answer.source == URL
    assert answer.failure == failure
    assert detail in answer.detail


@pytest.mark.asyncio
async def test_a_failure_after_the_ttl_answers_unavailable_not_the_old_catalog():
    clock = _Clock()
    reader = _reader(clock, ttl=60.0)
    with respx.mock() as http:
        http.get(URL).mock(side_effect=[httpx.Response(200, text=CATALOG), httpx.Response(500)])
        assert isinstance(await reader.read(), KitCatalog)
        clock.now += 61.0
        answer = await reader.read()

    assert isinstance(answer, KitCatalogUnavailable)
    assert answer.failure == KitCatalogFailure.STATUS


@pytest.mark.asyncio
async def test_a_failure_is_not_kept_and_the_next_read_tries_again():
    reader = _reader()
    with respx.mock() as http:
        route = http.get(URL).mock(
            side_effect=[httpx.ConnectError("down"), httpx.Response(200, text=CATALOG)]
        )
        assert isinstance(await reader.read(), KitCatalogUnavailable)
        assert isinstance(await reader.read(), KitCatalog)

    assert route.call_count == 2


@pytest.mark.asyncio
async def test_an_invalid_source_url_answers_unavailable():
    answer = await KitCatalogReader("not a url at all").read()

    assert isinstance(answer, KitCatalogUnavailable)
    assert answer.failure == KitCatalogFailure.TRANSPORT


def test_the_fetch_is_bounded():
    assert 0 < CATALOG_TIMEOUT_SECONDS <= 30


def test_the_url_is_the_kit_catalog_path_at_the_ref():
    assert catalog_url("https://raw.example/kit/", "HEAD") == (
        "https://raw.example/kit/HEAD/packages/catalog.yaml"
    )


def test_the_process_reader_reads_the_configured_source_and_ref(monkeypatch):
    monkeypatch.setenv("KIT_CATALOG_SOURCE", "https://raw.example/fork")
    monkeypatch.setenv("KIT_CATALOG_REF", "packages-preview")
    from src.config.settings import get_settings

    get_settings.cache_clear()
    get_kit_catalog_reader.cache_clear()
    try:
        assert get_kit_catalog_reader().url == (
            "https://raw.example/fork/packages-preview/packages/catalog.yaml"
        )
    finally:
        get_settings.cache_clear()
        get_kit_catalog_reader.cache_clear()


def test_by_default_the_reader_reads_the_kit_default_branch(monkeypatch):
    monkeypatch.delenv("KIT_CATALOG_SOURCE", raising=False)
    monkeypatch.delenv("KIT_CATALOG_REF", raising=False)
    from src.config.settings import get_settings

    get_settings.cache_clear()
    get_kit_catalog_reader.cache_clear()
    try:
        assert get_kit_catalog_reader().url == (
            "https://raw.githubusercontent.com/vladmesh/codegen-product-kit/HEAD/"
            "packages/catalog.yaml"
        )
    finally:
        get_settings.cache_clear()
        get_kit_catalog_reader.cache_clear()
