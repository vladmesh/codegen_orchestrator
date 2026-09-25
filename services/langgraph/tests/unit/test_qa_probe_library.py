"""The probe library a QA run offers, and the location seed run end to end through `qa probe`.

The HTTP-path tests run the real injected `qa` command in a child process
against a real capability endpoint, exactly as the executor container does. The
only stand-in is Telethon: a package on the child's path that records what it
was asked to do instead of reaching Telegram.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import re
import sys
from types import SimpleNamespace

import pytest

from shared.contracts.dto.qa_probe_library import (
    QA_PROBE_LIBRARY_CAP,
    QA_PROBE_LIBRARY_FILE_MAX,
    QA_PROBE_LIBRARY_FILE_PATTERN,
    QA_PROBE_LIBRARY_INDEX_MAX,
    QA_PROBE_LIBRARY_MAX_FILES,
    QA_PROBE_LIBRARY_NAME_MAX,
    QA_PROBE_LIBRARY_ORIGIN_MAX,
    QA_PROBE_LIBRARY_SEEDS_MAX,
    QA_PROBE_LIBRARY_USAGE_ARGS_MAX,
    QAProbeLibraryEntry,
    QAProbeLibraryFile,
    is_probe_library_name,
)
from shared.contracts.queues.worker import WorkerConfig, WorkerOwnership
from shared.qa_probe_cli import QA_PROBE_LIBRARY_PATH, QA_PROBE_SCRIPT
from shared.qa_probe_library import _SEEDS, seed_probes
from src.agents.qa.capability_service import QACapabilityService
from src.agents.qa.tools import build_qa_callables
from src.consumers._qa_probe_library import build_probe_library, prepare_probe_library
from src.consumers._qa_target import QACapabilities, QATarget, QATargetSession
from src.consumers._qa_telegram_identity import redact
from src.consumers._qa_workspace import MAX_PROBES, qa_workspace

PROJECT = "116c9678-5872-4ce5-8332-9a267ab27604"
OTHER_PROJECT = "0b6f5a9e-0000-4000-8000-000000000002"
SESSION = "1BVtsOHcBu-string-session-secret"
API_HASH = "0123456789abcdef-api-hash"
TARGET = QATarget(
    server_ip="1.2.3.4",
    ssh_user="root",
    qa_ssh_user="qa-observer",
    server_handle="vps-1",
    project_name="weather-bot",
    deployed_url="http://1.2.3.4:8000",
    allocated_ports=frozenset({8000}),
    bot_username="weather_bot",
)
CAPABILITIES = QACapabilities(
    deployed_url=TARGET.deployed_url,
    physical_root="/srv/deployments/weather-bot",
    containers=frozenset({"weather-bot-backend-1"}),
    loopback_ports=frozenset({8000}),
)

# A Telethon that records instead of connecting. It logs its own import, so
# "refused before any Telethon call" is read off the log, not assumed.
FAKE_TELETHON = {
    "telethon/__init__.py": """
import json, os
def _log(event):
    with open(os.environ["FAKE_TELETHON_LOG"], "a") as handle:
        handle.write(json.dumps(event) + "\\n")
_log({"event": "import"})
""",
    "telethon/sessions.py": """
class StringSession:
    def __init__(self, value):
        self.value = value
""",
    "telethon/tl/__init__.py": "",
    "telethon/tl/types.py": """
class InputGeoPoint:
    def __init__(self, lat, long):
        self.lat, self.long = lat, long
class InputMediaGeoPoint:
    def __init__(self, geo_point):
        self.geo_point = geo_point
""",
    "telethon/sync.py": """
from telethon import _log
class _Message:
    def __init__(self, id, out, raw_text=None):
        self.id, self.out, self.raw_text = id, out, raw_text
        self.media = None
        self.reply_markup = None
class TelegramClient:
    def __init__(self, session, api_id, api_hash, proxy=None):
        _log({"event": "client", "session": session.value, "api_id": api_id,
              "api_hash": api_hash, "proxy": list(proxy) if proxy else None})
    def connect(self):
        _log({"event": "connect"})
    def is_user_authorized(self):
        return True
    def get_entity(self, name):
        _log({"event": "get_entity", "name": name})
        return name
    def send_file(self, entity, media):
        point = media.geo_point
        _log({"event": "send_file", "entity": entity, "lat": point.lat, "long": point.long,
              "types": [type(point.lat).__name__, type(point.long).__name__]})
        return _Message(100, True)
    def get_messages(self, entity, min_id=0, limit=10):
        return [_Message(101, False, "Location received: Moscow"), _Message(100, True)]
    def disconnect(self):
        _log({"event": "disconnect"})
""",
}


class FakeConn:
    async def run(self, command, *, check=False, timeout=None):
        return SimpleNamespace(exit_status=0, stdout="", stderr="")


def _entry(name: str, *, project_id: str = PROJECT, platform: str = "http", **overrides):
    values = {
        "project_id": project_id,
        "platform": platform,
        "name": name,
        "source": f"print({name!r})",
        "file_kind": "py",
        "origin_run_id": "qa-run-7",
        "created_at": datetime.now(UTC),
        "updated_at": datetime.now(UTC),
    }
    values.update(overrides)
    return QAProbeLibraryEntry(**values)


@pytest.fixture
def sandbox(tmp_path):
    """A container's view: its `qa` command, its library, a home and a stub Telethon."""
    stub = tmp_path / "stub"
    for relative, body in FAKE_TELETHON.items():
        path = stub / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
    qa = tmp_path / "workspace" / "qa"
    qa.parent.mkdir()
    qa.write_text(QA_PROBE_SCRIPT)
    library_root = tmp_path / "workspace" / "qa-library"
    library = build_probe_library(
        project_id=PROJECT, seeds=seed_probes(telegram_bot=True), entries=[], read_failure=None
    )
    for item in library.files:
        (library_root / item.path).parent.mkdir(parents=True, exist_ok=True)
        (library_root / item.path).write_text(item.content)
    home = tmp_path / "home"
    home.mkdir()
    return SimpleNamespace(
        qa=qa,
        library=library_root,
        home=home,
        stub=stub,
        log=tmp_path / "telethon.log",
        workdir=tmp_path,
    )


@pytest.fixture
async def endpoint(tmp_path):
    with qa_workspace(root=str(tmp_path / "runs")) as workspace:
        service = QACapabilityService(
            calls=build_qa_callables(
                session=QATargetSession(TARGET, FakeConn(), CAPABILITIES), workspace=workspace
            ),
            capabilities=CAPABILITIES.describe(),
            submit_verdict=workspace.submit_verdict,
            advertised_host="127.0.0.1",
            telegram_identity={
                "TELETHON_API_ID": "12345",
                "TELETHON_API_HASH": API_HASH,
                "TELETHON_SESSION": SESSION,
            },
            probe_secrets=(SESSION, API_HASH),
            redact_text=redact,
        )
        started = await service.start()
        try:
            yield SimpleNamespace(url=started.url, token=started.token, workspace=workspace)
        finally:
            await service.stop()


async def _qa(sandbox, endpoint, *argv: str) -> tuple[int, str, str]:
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(sandbox.home),
        "PYTHONPATH": str(sandbox.stub),
        "QA_CAPABILITY_URL": endpoint.url,
        "QA_CAPABILITY_TOKEN": endpoint.token,
        "HTTPS_PROXY": "http://qa-egress-proxy:3128",
        "FAKE_TELETHON_LOG": str(sandbox.log),
    }
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        str(sandbox.qa),
        *argv,
        env=env,
        cwd=str(sandbox.workdir),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=60)
    return process.returncode, stdout.decode(), stderr.decode()


def _telethon_log(sandbox) -> list[dict]:
    if not sandbox.log.exists():
        return []
    return [json.loads(line) for line in sandbox.log.read_text().splitlines()]


class TestTheLocationSeedThroughQaProbe:
    async def test_a_location_is_sent_as_values_and_the_probe_is_retained(self, sandbox, endpoint):
        status, _, _ = await _qa(sandbox, endpoint, "telegram_identity")
        assert status == 0
        seed = sandbox.library / "telegram" / "location.py"

        status, stdout, _ = await _qa(
            sandbox,
            endpoint,
            "probe",
            "telegram",
            "location",
            str(seed),
            "@weather_bot",
            "55.7558",
            "37.6173",
            "0",
        )

        assert status == 0
        assert stdout.startswith("probe-1\n")
        [record] = endpoint.workspace.probe_runs
        assert record.platform == "telegram"
        assert record.name == "location"
        assert record.file_kind == "py"
        assert record.arguments == ["@weather_bot", "55.7558", "37.6173", "0"]
        assert record.exit_status == 0
        assert record.source == seed.read_text()
        assert record.source_truncated is False
        answer = json.loads(record.stdout)
        assert answer["delivered"] is True
        assert answer["error"] is None
        assert answer["latitude"] == 55.7558
        assert [reply["text"] for reply in answer["replies"]] == ["Location received: Moscow"]
        events = {event["event"]: event for event in _telethon_log(sandbox)}
        assert events["client"]["proxy"] == ["http", "qa-egress-proxy", 3128]
        assert events["client"]["session"] == SESSION
        assert events["send_file"]["entity"] == "@weather_bot"
        assert (events["send_file"]["lat"], events["send_file"]["long"]) == (55.7558, 37.6173)
        assert events["send_file"]["types"] == ["float", "float"]
        for text in (record.source, record.stdout, record.stderr, stdout):
            assert SESSION not in text
            assert API_HASH not in text

    @pytest.mark.parametrize(
        "latitude",
        ["1); import os; os.system('touch PWNED')#", "nan", "inf", "north", "91"],
    )
    async def test_a_refused_coordinate_is_retained_as_a_failed_probe_and_sends_nothing(
        self, sandbox, endpoint, latitude
    ):
        assert (await _qa(sandbox, endpoint, "telegram_identity"))[0] == 0

        status, _, stderr = await _qa(
            sandbox,
            endpoint,
            "probe",
            "telegram",
            "location",
            str(sandbox.library / "telegram" / "location.py"),
            "@weather_bot",
            latitude,
            "37.6",
        )

        assert status == 2
        assert "location probe refused" in stderr
        [record] = endpoint.workspace.probe_runs
        assert record.exit_status == 2
        assert record.arguments == ["@weather_bot", latitude, "37.6"]
        assert _telethon_log(sandbox) == []
        assert not (sandbox.workdir / "PWNED").exists()

    async def test_without_a_proven_identity_the_probe_exits_clearly(self, sandbox, endpoint):
        status, _, stderr = await _qa(
            sandbox,
            endpoint,
            "probe",
            "telegram",
            "location",
            str(sandbox.library / "telegram" / "location.py"),
            "@weather_bot",
            "55.7",
            "37.6",
        )

        assert status == 3
        assert "qa telegram_identity" in stderr
        [record] = endpoint.workspace.probe_runs
        assert record.exit_status == 3
        assert _telethon_log(sandbox) == []

    async def test_a_control_character_heavy_probe_is_retained_not_dropped(self, sandbox, endpoint):
        probe = sandbox.workdir / "control.py"
        probe.write_text(
            "# " + "\x03" * 19000 + "\nimport sys\n"
            "sys.stdout.write(chr(1) * 19000)\nsys.stderr.write(chr(2) * 19000)\n"
        )

        status, stdout, _ = await _qa(sandbox, endpoint, "probe", "http", "control", str(probe))

        assert status == 0
        assert stdout.startswith("probe-1\n")
        [record] = endpoint.workspace.probe_runs
        assert record.source_truncated and record.stdout_truncated and record.stderr_truncated
        assert record.stdout.startswith("\x01" * 1000)


class TestTheOfferedLibrary:
    def test_seeds_come_only_with_a_bot_and_the_index_lists_every_file(self):
        without_bot = build_probe_library(
            project_id=PROJECT, seeds=seed_probes(telegram_bot=False), entries=[], read_failure=None
        )
        with_bot = build_probe_library(
            project_id=PROJECT,
            seeds=seed_probes(telegram_bot=True),
            entries=[_entry("health")],
            read_failure=None,
        )

        assert [item.path for item in without_bot.files] == ["index.json"]
        assert json.loads(without_bot.files[0].content) == {"probes": []}
        paths = [item.path for item in with_bot.files]
        assert paths == ["telegram/location.py", "http/health.py", "index.json"]
        index = json.loads(with_bot.files[-1].content)["probes"]
        assert [(row["name"], row["platform"], row["origin"]) for row in index] == [
            ("location", "telegram", "seed"),
            ("health", "http", "qa-run-7"),
        ]
        assert index[0]["usage"] == (
            f"qa probe telegram location {QA_PROBE_LIBRARY_PATH}/telegram/location.py "
            "@BOT LAT LON [WAIT_SECONDS]"
        )
        assert index[1]["usage"] == (
            f"qa probe http health {QA_PROBE_LIBRARY_PATH}/http/health.py [ARG ...]"
        )
        assert [(item.name, item.origin) for item in with_bot.offer.offered] == [
            ("location", "seed"),
            ("health", "qa-run-7"),
        ]

    def test_another_projects_entry_is_never_offered(self):
        library = build_probe_library(
            project_id=PROJECT,
            seeds=[],
            entries=[_entry("mine"), _entry("theirs", project_id=OTHER_PROJECT)],
            read_failure=None,
        )

        assert [item.name for item in library.offer.offered] == ["mine"]
        assert "theirs" not in library.files[-1].content

    def test_a_seed_shadows_a_stored_entry_of_the_same_name(self):
        library = build_probe_library(
            project_id=PROJECT,
            seeds=seed_probes(telegram_bot=True),
            entries=[_entry("location", platform="telegram", source="print('stale copy')")],
            read_failure=None,
        )

        [location] = [item for item in library.files if item.path == "telegram/location.py"]
        assert location.content == seed_probes(telegram_bot=True)[0].source()
        assert [item.origin for item in library.offer.offered] == ["seed"]

    @pytest.mark.parametrize("name", ["health", "a_b-c8687a08", "health.v2", "x" * 64])
    def test_a_library_name_is_its_own_file_stem(self, name):
        library = build_probe_library(
            project_id=PROJECT, seeds=[], entries=[_entry(name)], read_failure=None
        )

        assert library.files[0].path == f"http/{name}.py"
        assert re.fullmatch(QA_PROBE_LIBRARY_FILE_PATTERN, library.files[0].path)

    async def test_a_failed_read_offers_the_seeds_and_says_why(self):
        async def unreadable(_project_id):
            raise ConnectionError("api unreachable")

        library = await prepare_probe_library(
            project_id=PROJECT, telegram_bot=True, read_entries=unreadable
        )

        assert [item.path for item in library.files] == ["telegram/location.py", "index.json"]
        assert "api unreachable" in library.offer.read_failure

    def test_a_full_library_fits_one_create_request(self):
        entries = [
            _entry(f"{index:03}".ljust(QA_PROBE_LIBRARY_NAME_MAX, "n"), source="x" * 20_000)
            for index in range(QA_PROBE_LIBRARY_CAP)
        ]
        library = build_probe_library(
            project_id=PROJECT,
            seeds=seed_probes(telegram_bot=True),
            entries=entries,
            read_failure=None,
        )

        config = _worker_config(library.files)
        assert len(config.qa_probe_library) == QA_PROBE_LIBRARY_CAP + 2

    def test_the_index_of_any_stored_library_is_bounded_by_arithmetic(self):
        # The worst a store can write: the cap of longest names, each from a
        # Run id as wide as `runs.id` made of characters JSON escapes sixfold.
        entries = [
            _entry(
                f"{index:03}".ljust(QA_PROBE_LIBRARY_NAME_MAX, "n"),
                origin_run_id="\x01" * QA_PROBE_LIBRARY_ORIGIN_MAX,
            )
            for index in range(QA_PROBE_LIBRARY_CAP)
        ]
        library = build_probe_library(
            project_id=PROJECT,
            seeds=seed_probes(telegram_bot=True),
            entries=entries,
            read_failure=None,
        )

        rows = len(library.offer.offered)
        row_max = 256 + 4 * QA_PROBE_LIBRARY_NAME_MAX + 6 * QA_PROBE_LIBRARY_ORIGIN_MAX + 64
        assert QA_PROBE_LIBRARY_INDEX_MAX == 64 + (
            QA_PROBE_LIBRARY_CAP + QA_PROBE_LIBRARY_SEEDS_MAX
        ) * (row_max)
        assert len(library.files[-1].content) <= 64 + rows * row_max
        _worker_config(library.files)

    def test_every_seed_fits_the_arithmetic(self):
        assert len(_SEEDS) <= QA_PROBE_LIBRARY_SEEDS_MAX
        for seed in _SEEDS:
            assert is_probe_library_name(seed.name)
            assert len(seed.arguments) <= QA_PROBE_LIBRARY_USAGE_ARGS_MAX

    @pytest.mark.parametrize("telegram_bot", [True, False])
    def test_the_seeds_alone_always_build(self, telegram_bot):
        library = build_probe_library(
            project_id=PROJECT,
            seeds=seed_probes(telegram_bot=telegram_bot),
            entries=[],
            read_failure=None,
        )

        _worker_config(library.files)

    def test_the_cap_holds_a_whole_run_of_probes(self):
        # A passed run's own entries never evict each other.
        assert QA_PROBE_LIBRARY_CAP >= MAX_PROBES


def _worker_config(files) -> WorkerConfig:
    return WorkerConfig(
        name="qa-1",
        worker_type="qa",
        agent_type="claude",
        instructions="rules",
        allowed_commands=["*"],
        capabilities=["qa_sandbox"],
        ownership=WorkerOwnership(project_id=PROJECT, run_id="r", attempt_id="a"),
        qa_probe_library=files,
    )


def _named(*names: str) -> list[QAProbeLibraryEntry]:
    return [_entry(name) for name in names]


# What a DB can return that no store under the name rule writes: rows from
# before it, or from any other path. None of it may reach the create request.
UNBUILDABLE_LIBRARIES = {
    # The first review's two reproductions.
    "reviewer-collision": _named("a b", "a_b-c8687a08"),
    "reviewer-overflow": _named(*("'" * 250 + f"{index:03}" for index in range(50))),
    "control-character-names": _named(*("\x01" * 250 + f"{index:03}" for index in range(20))),
    "repeated-name": _named("health", "health"),
    "path-traversal": _named("../../etc/passwd"),
    "too-long-name": _named("x" * (QA_PROBE_LIBRARY_NAME_MAX + 1)),
    "too-many-entries": _named(*(f"p{index}" for index in range(QA_PROBE_LIBRARY_MAX_FILES))),
    "index-over-budget": [
        _entry(f"p{index}", origin_run_id="o" * 20_000) for index in range(QA_PROBE_LIBRARY_CAP)
    ],
    "oversized-source": [_entry("big", source="x" * (QA_PROBE_LIBRARY_FILE_MAX + 1))],
}


class TestAStoredLibraryNeverFailsARun:
    @pytest.mark.parametrize("telegram_bot", [True, False])
    @pytest.mark.parametrize("case", sorted(UNBUILDABLE_LIBRARIES))
    async def test_an_unbuildable_library_degrades_to_the_seeds_with_a_note(
        self, case, telegram_bot
    ):
        entries = UNBUILDABLE_LIBRARIES[case]

        async def read(_project_id):
            return entries

        library = await prepare_probe_library(
            project_id=PROJECT, telegram_bot=telegram_bot, read_entries=read
        )

        seeds = build_probe_library(
            project_id=PROJECT,
            seeds=seed_probes(telegram_bot=telegram_bot),
            entries=[],
            read_failure=None,
        )
        assert [item.path for item in library.files] == [item.path for item in seeds.files]
        assert library.files[-1].content == seeds.files[-1].content
        assert library.offer.offered == seeds.offer.offered
        assert library.offer.read_failure is None
        assert library.offer.build_failure.startswith("the project's probe library could not")
        assert len(library.offer.build_failure) <= 500
        # The executor's create request still builds.
        _worker_config(library.files)

    def test_the_build_itself_refuses_what_the_create_request_would(self):
        for entries in UNBUILDABLE_LIBRARIES.values():
            with pytest.raises(ValueError):
                build_probe_library(
                    project_id=PROJECT, seeds=[], entries=entries, read_failure=None
                )

    async def test_a_read_and_a_build_failure_are_told_apart(self):
        async def read(_project_id):
            return _named("health")

        library = await prepare_probe_library(
            project_id=PROJECT, telegram_bot=False, read_entries=read
        )

        assert [item.name for item in library.offer.offered] == ["health"]
        assert library.offer.read_failure is None
        assert library.offer.build_failure is None


class TestTheWorkerContract:
    def _config(self, **overrides) -> WorkerConfig:
        values = {
            "name": "qa-1",
            "worker_type": "qa",
            "agent_type": "claude",
            "instructions": "rules",
            "allowed_commands": ["*"],
            "capabilities": ["qa_sandbox"],
            "ownership": WorkerOwnership(project_id=PROJECT, run_id="r", attempt_id="a"),
        }
        values.update(overrides)
        return WorkerConfig(**values)

    @pytest.mark.parametrize(
        "path",
        ["../qa", "telegram/../../x.py", "/etc/passwd", "telegram/a/b.py", "db/x.py", "http/x.exe"],
    )
    def test_a_path_outside_the_library_shape_is_refused(self, path):
        with pytest.raises(ValueError, match="pattern"):
            QAProbeLibraryFile(path=path, content="")

    def test_a_developer_worker_is_offered_no_library(self):
        with pytest.raises(ValueError, match="only a qa worker"):
            self._config(
                worker_type="developer",
                qa_probe_library=[QAProbeLibraryFile(path="index.json", content="{}")],
            )

    def test_duplicate_paths_are_refused(self):
        item = QAProbeLibraryFile(path="http/a.py", content="")
        with pytest.raises(ValueError, match="unique"):
            self._config(qa_probe_library=[item, item])


def test_the_seed_file_is_where_the_index_says(sandbox):
    index = json.loads((sandbox.library / "index.json").read_text())["probes"]
    [row] = index
    assert Path(row["file"]).relative_to(QA_PROBE_LIBRARY_PATH) == Path("telegram/location.py")
    assert (sandbox.library / "telegram" / "location.py").is_file()
