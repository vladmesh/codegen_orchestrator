"""One model turn as one subscription-CLI invocation (`codex`, `claude`).

The serialized conversation — system, human, AI with tool calls, tool results —
and the bound tools' names, descriptions and argument schemas go in on stdin,
never argv (one argv string is capped at 128 KiB). A strict output schema
constrains the answer to ``{content, tool_calls: [{name, arguments_json}]}``;
the arguments are a JSON string because one strict schema cannot carry every
tool's argument shape, so they are parsed here and validated against the bound
tool's own schema. The answer becomes an `AIMessage` with generated tool-call
ids, or plain content.

The process runs in an empty temporary directory with no tools of its own and a
minimal environment: PATH, locale, its own credential variable and a HOME of its
own that is deleted with the call, so nothing persists between calls. No API
key, Redis or database URL of this process ever reaches it. It never runs as
root: `codex` runs as the owner of the profile directory, so a token refresh
leaves `auth.json` owned by the uid the Codex workers read it as; `claude` runs
as `nobody`. Codex invocations against one `CODEX_HOME` are serialized by the
profile's advisory lock, the same `.codegen-codex.lock` the worker wrapper and
worker-manager's profile reader use, so two refreshes can never rewrite
`auth.json` at once. The profile is used in place and never copied: a copy that
refreshes rotates the refresh token and breaks the original.

Everything that stops a channel from answering raises `ChannelFailure`. Output
that is not schema-valid, names an unbound tool or carries arguments that fail
the tool's schema gets one corrective re-ask on the same channel first. The
call's deadline is the chain's: its cancellation kills the process group.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
import fcntl
import json
import os
from pathlib import Path
import pwd
import shutil
import signal
import stat
import tempfile
import tomllib
from typing import Any
import uuid

from langchain_core.callbacks import AsyncCallbackManagerForLLMRun, CallbackManagerForLLMRun
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import BaseTool
from langchain_core.utils.function_calling import convert_to_openai_tool
from pydantic import BaseModel, ConfigDict, SecretStr, ValidationError

from shared.contracts.dto.llm_channel import LLMChannel

from .errors import ChannelFailure, ChannelFailureClass, classify_error_text, short_reason

#: The fixed answer shape both CLIs are held to.
TURN_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["content", "tool_calls"],
    "properties": {
        "content": {"type": "string"},
        "tool_calls": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["name", "arguments_json"],
                "properties": {
                    "name": {"type": "string"},
                    "arguments_json": {"type": "string"},
                },
            },
        },
    },
}

#: The only variables of this process a CLI child inherits; credentials are added per CLI.
CHILD_ENV_ALLOWLIST = (
    "HOME",
    "PATH",
    "LANG",
    "LANGUAGE",
    "LC_ALL",
    "LC_CTYPE",
    "LC_MESSAGES",
    "TZ",
)

#: The advisory lock `worker_wrapper.wrapper.codex_profile_lock` holds for a Codex turn.
CODEX_PROFILE_LOCK_NAME = ".codegen-codex.lock"
_LOCK_POLL_SECONDS = 0.1

#: The modes worker-manager requires of a Codex profile (`codex_auth.py`).
_PRIVATE_DIRECTORY_MODE = 0o700
_PRIVATE_FILE_MODE = 0o600

#: Who a `claude` child runs as when this process is root: it owns no file of its own.
CLAUDE_CHILD_USER = "nobody"

#: How long `--version` may take in the startup readiness probe.
_VERSION_PROBE_SECONDS = 30.0

#: How much of a rejected answer goes back to the model with the corrective re-ask.
_REJECTED_ANSWER_ECHO = 4000
_ERROR_TAIL = 2000
_REASON_TAIL = 240

_PROMPT_HEADER = """\
You are the model behind one assistant turn of the conversation below. Produce the
assistant's next message and nothing else.

Answer with exactly one JSON object that matches the output schema:
- "tool_calls": the tool calls of this turn, possibly none. Each "name" is one of
  the tools listed below, and "arguments_json" is a JSON object, encoded as a
  string, that matches that tool's "parameters" schema.
- "content": the assistant's text. It may be empty when the turn calls tools.

The tools below are the only tools that exist. Do not run commands, read files or
use any capability of your own: calling a listed tool is the only way to act.
"""


class InvalidTurn(Exception):  # noqa: N818 - an invalid answer, answered with one re-ask
    """The CLI answered, but not with a turn this conversation can use."""

    def __init__(self, problem: str, rejected: str = "") -> None:
        super().__init__(problem)
        self.rejected = rejected


class _TurnCall(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    arguments_json: str


class _TurnAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: str
    tool_calls: list[_TurnCall]


def _text(message: BaseMessage) -> str:
    return str(message.text)


def _message_entry(message: BaseMessage) -> dict[str, Any]:
    if isinstance(message, SystemMessage):
        return {"role": "system", "content": _text(message)}
    if isinstance(message, HumanMessage):
        return {"role": "user", "content": _text(message)}
    if isinstance(message, AIMessage):
        entry: dict[str, Any] = {"role": "assistant", "content": _text(message)}
        if message.tool_calls:
            entry["tool_calls"] = [
                {"id": call["id"], "name": call["name"], "arguments": call["args"]}
                for call in message.tool_calls
            ]
        return entry
    if isinstance(message, ToolMessage):
        return {
            "role": "tool",
            "tool_call_id": message.tool_call_id,
            "name": message.name,
            "status": message.status,
            "content": _text(message),
        }
    return {"role": message.type, "content": _text(message)}


class BoundTools:
    """The tools a turn may call: their specs for the prompt, their validators for the answer."""

    def __init__(self, tools: Sequence[Any]) -> None:
        self.specs: list[dict[str, Any]] = []
        self._validators: dict[str, Callable[[dict], Any]] = {}
        for tool in tools:
            function = convert_to_openai_tool(tool)["function"]
            self.specs.append(
                {
                    "name": function["name"],
                    "description": function.get("description", ""),
                    "parameters": function.get("parameters", {}),
                }
            )
            self._validators[function["name"]] = _validator(tool)

    def render_prompt(self, messages: list[BaseMessage]) -> str:
        conversation = [_message_entry(message) for message in messages]
        return (
            f"{_PROMPT_HEADER}\nTOOLS (JSON):\n{json.dumps(self.specs, ensure_ascii=False)}\n\n"
            "CONVERSATION (JSON, oldest message first):\n"
            f"{json.dumps(conversation, ensure_ascii=False)}\n"
        )

    def parse(self, answer: Any) -> AIMessage:
        """The answer as an `AIMessage`, or `InvalidTurn` saying what is wrong with it."""
        try:
            return self._parse(answer)
        except InvalidTurn as problem:
            problem.rejected = problem.rejected or json.dumps(answer, default=str)
            raise

    def _parse(self, answer: Any) -> AIMessage:
        try:
            turn = _TurnAnswer.model_validate(answer)
        except ValidationError as exc:
            raise InvalidTurn(
                f"the answer does not match the output schema: {_errors(exc)}"
            ) from None
        tool_calls = []
        for call in turn.tool_calls:
            validate = self._validators.get(call.name)
            if validate is None:
                raise InvalidTurn(f"tool {call.name!r} is not one of the listed tools")
            try:
                arguments = json.loads(call.arguments_json)
            except json.JSONDecodeError:
                raise InvalidTurn(
                    f"arguments_json of tool {call.name!r} is not valid JSON"
                ) from None
            if not isinstance(arguments, dict):
                raise InvalidTurn(f"arguments_json of tool {call.name!r} is not a JSON object")
            try:
                validate(arguments)
            except ValidationError as exc:
                raise InvalidTurn(
                    f"arguments of tool {call.name!r} fail its schema: {_errors(exc)}"
                ) from None
            tool_calls.append(
                {
                    "name": call.name,
                    "args": arguments,
                    "id": f"call_{uuid.uuid4().hex[:24]}",
                    "type": "tool_call",
                }
            )
        return AIMessage(content=turn.content, tool_calls=tool_calls)


def _validator(tool: Any) -> Callable[[dict], Any]:
    schema: Any = tool.tool_call_schema if isinstance(tool, BaseTool) else tool
    if isinstance(schema, type) and issubclass(schema, BaseModel):
        return schema.model_validate
    return lambda arguments: arguments


def _errors(exc: ValidationError) -> str:
    return "; ".join(
        f"{'.'.join(str(part) for part in error['loc']) or '<root>'}: {error['msg']}"
        for error in exc.errors(include_url=False, include_input=False)[:5]
    )


def _corrective(prompt: str, problem: InvalidTurn) -> str:
    return (
        f"{prompt}\nYOUR PREVIOUS ANSWER TO THIS TURN WAS REJECTED: {problem}\n"
        f"The rejected answer was:\n{problem.rejected[:_REJECTED_ANSWER_ECHO]}\n"
        "Answer this turn again, as one JSON object matching the output schema.\n"
    )


def child_env(credentials: dict[str, str]) -> dict[str, str]:
    """The CLI's whole environment: the allowlist of this process plus its own credential."""
    env = {name: os.environ[name] for name in CHILD_ENV_ALLOWLIST if name in os.environ}
    env.update(credentials)
    return env


@dataclass(frozen=True)
class ChildUser:
    """The uid and gid a CLI child runs as.

    Only root switches: it runs the child as this user with no supplementary
    groups (root's own would otherwise be inherited) and hands it the call's
    temporary files. Any other process may only run a child as itself.
    """

    uid: int
    gid: int

    @classmethod
    def switchable(cls, uid: int, gid: int, what: str) -> ChildUser:
        """This user, or `ChannelFailure(MISSING_CREDENTIAL)` when this process cannot be it."""
        euid = os.geteuid()
        if euid not in (0, uid):
            raise ChannelFailure(
                ChannelFailureClass.MISSING_CREDENTIAL,
                f"{what} is owned by uid {uid}; this process runs as uid {euid} "
                "and cannot run the CLI as that uid",
            )
        return cls(uid, gid)

    def popen_kwargs(self) -> dict[str, Any]:
        if os.geteuid() != 0:
            return {}
        return {"user": self.uid, "group": self.gid, "extra_groups": []}

    def own(self, *paths: Path) -> None:
        if os.geteuid() != 0:
            return
        for path in paths:
            os.chown(path, self.uid, self.gid)


class CliTurnChatModel(BaseChatModel):
    """Shared turn loop; subclasses name the command line and read the result."""

    channel: LLMChannel
    binary: str
    model: str | None = None
    tools: list[Any] = []

    @property
    def _llm_type(self) -> str:
        return f"{self.channel.value}-cli-turn"

    def bind_tools(
        self, tools: Sequence[Any], *, tool_choice: str | None = None, **kwargs: Any
    ) -> CliTurnChatModel:
        """Tools are part of the prompt; `tool_choice` and provider kwargs do not apply."""
        return self.model_copy(update={"tools": list(tools)})

    # --- per-CLI hooks -------------------------------------------------------

    def _credentials(self) -> dict[str, str]:
        """This CLI's credential variables, or `ChannelFailure(MISSING_CREDENTIAL)`."""
        raise NotImplementedError

    def _child_user(self) -> ChildUser:
        """Who the CLI runs as, or `ChannelFailure(MISSING_CREDENTIAL)`."""
        raise NotImplementedError

    def _settings_env(self) -> dict[str, str]:
        """Fixed, non-secret variables this CLI needs besides its credential."""
        return {}

    def _command(self, executable: str, workdir: Path, io_dir: Path) -> list[str]:
        raise NotImplementedError

    def _answer(self, stdout: bytes, io_dir: Path) -> Any:
        """The decoded answer object of a successful run, or `InvalidTurn`."""
        raise NotImplementedError

    def _reported_error(self, stdout: bytes) -> str | None:
        """An error the CLI reported inside a zero-exit result, if it has that notion."""
        return None

    @asynccontextmanager
    async def _serialized(self, child: ChildUser) -> AsyncIterator[None]:
        yield

    # --- readiness -----------------------------------------------------------

    async def readiness(self) -> tuple[ChannelFailure | None, str | None]:
        """Whether a call could start now, and the installed CLI's version.

        Checks what a call checks before it starts the CLI — credential,
        profile, child user, binary — and runs `--version` in a throwaway HOME
        with no credential, so the probe never touches a profile.
        """
        failure: ChannelFailure | None = None
        try:
            self._child_user()
            self._credentials()
        except ChannelFailure as exc:
            failure = exc
        version, probe_failure = await self._version()
        return failure or probe_failure, version

    async def _version(self) -> tuple[str | None, ChannelFailure | None]:
        path = os.environ.get("PATH", "")
        executable = shutil.which(self.binary, path=path)
        if executable is None:
            return None, ChannelFailure(
                ChannelFailureClass.BINARY_MISSING, f"{self.binary} is not on PATH"
            )
        with tempfile.TemporaryDirectory(prefix=f"llm-{self.channel.value}-version-") as home:
            try:
                returncode, stdout, stderr = await asyncio.wait_for(
                    _communicate(
                        [executable, "--version"],
                        b"",
                        cwd=Path(home),
                        env={"PATH": path, "HOME": home},
                    ),
                    _VERSION_PROBE_SECONDS,
                )
            except (TimeoutError, OSError) as exc:
                return None, ChannelFailure(
                    ChannelFailureClass.NONZERO_EXIT, f"{self.binary} --version: {exc!r}"
                )
        lines = stdout.decode(errors="replace").strip().splitlines()
        if returncode != 0 or not lines:
            return None, ChannelFailure(
                ChannelFailureClass.NONZERO_EXIT,
                f"{self.binary} --version exit {returncode}: "
                f"{stderr.decode(errors='replace')[-_REASON_TAIL:]}",
            )
        return short_reason(lines[0]), None

    # --- the turn ------------------------------------------------------------

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        raise NotImplementedError("a CLI turn is async-only: its deadline is the event loop's")

    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        tools = BoundTools(self.tools)
        prompt = tools.render_prompt(messages)
        try:
            message = tools.parse(await self._run(prompt))
        except InvalidTurn as problem:
            try:
                message = tools.parse(await self._run(_corrective(prompt, problem)))
            except InvalidTurn as again:
                raise ChannelFailure(
                    ChannelFailureClass.INVALID_OUTPUT, f"after one corrective re-ask: {again}"
                ) from None
        message.response_metadata = {"model_name": self.model or f"{self.channel.value}-default"}
        return ChatResult(generations=[ChatGeneration(message=message)])

    async def _run(self, prompt: str) -> Any:
        """One CLI invocation: the answer object, `InvalidTurn`, or `ChannelFailure`."""
        child = self._child_user()
        credentials = self._credentials()
        env = child_env(credentials)
        executable = shutil.which(self.binary, path=env.get("PATH", ""))
        if executable is None:
            raise ChannelFailure(
                ChannelFailureClass.BINARY_MISSING, f"{self.binary} is not on PATH"
            )
        with tempfile.TemporaryDirectory(prefix=f"llm-{self.channel.value}-") as root:
            workdir = Path(root, "work")
            io_dir = Path(root, "io")
            home = Path(root, "home")
            for directory in (workdir, io_dir, home):
                directory.mkdir()
            env.update(self._settings_env())
            env["HOME"] = str(home)
            command = self._command(executable, workdir, io_dir)
            child.own(Path(root), workdir, io_dir, home, *io_dir.iterdir())
            async with self._serialized(child):
                returncode, stdout, stderr = await _communicate(
                    command, prompt.encode(), cwd=workdir, env=env, child=child
                )
            secrets = tuple(credentials.values())
            if returncode != 0:
                # The error is at the end; a CLI may echo the conversation before it,
                # and that is neither evidence for the class nor a log field.
                text = f"{stdout.decode(errors='replace')}\n{stderr.decode(errors='replace')}"
                tail = text.strip()[-_ERROR_TAIL:]
                raise ChannelFailure(
                    classify_error_text(tail),
                    f"exit {returncode}: {_redacted(tail, secrets)[-_REASON_TAIL:]}",
                )
            reported = self._reported_error(stdout)
            if reported is not None:
                raise ChannelFailure(
                    classify_error_text(reported[-_ERROR_TAIL:]),
                    _redacted(reported, secrets)[-_REASON_TAIL:],
                )
            try:
                return self._answer(stdout, io_dir)
            except InvalidTurn as problem:
                problem.rejected = problem.rejected or stdout.decode(errors="replace")
                raise


def _redacted(text: str, secrets: tuple[str, ...]) -> str:
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[redacted]")
    return text


async def _communicate(
    command: list[str],
    stdin: bytes,
    *,
    cwd: Path,
    env: dict[str, str],
    child: ChildUser | None = None,
) -> tuple[int, bytes, bytes]:
    """Run to completion; a cancelled call (the chain's deadline) kills the process group."""
    process = await asyncio.create_subprocess_exec(
        *command,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
        env=env,
        start_new_session=True,
        **(child.popen_kwargs() if child is not None else {}),
    )
    try:
        stdout, stderr = await process.communicate(stdin)
    except BaseException:
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        with suppress(BaseException):
            await process.wait()
        raise
    assert process.returncode is not None
    return process.returncode, stdout, stderr


def _json_object(text: str, what: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        raise InvalidTurn(f"{what} is not JSON", rejected=text) from None


def _unsuitable(reason: str) -> ChannelFailure:
    return ChannelFailure(ChannelFailureClass.MISSING_CREDENTIAL, reason)


def codex_profile_owner(codex_home: str | None) -> ChildUser:
    """The owner of a usable Codex profile, or why the profile is unusable.

    The profile is the one the Codex workers mount (`HOST_CODEX_HOME`), held to
    worker-manager's rules: a directory of mode 0700, a non-empty `auth.json`
    and a `config.toml` of mode 0600 that sets `cli_auth_credentials_store =
    "file"`. Beyond those, its owner must not be root and must own `auth.json`:
    the CLI runs as that owner, so a refresh writes a file the workers can read.
    Nothing here reads the tokens.
    """
    if not codex_home:
        raise _unsuitable("LLM_CODEX_HOME is not set")
    profile = Path(codex_home)
    try:
        directory = profile.stat()
    except OSError:
        raise _unsuitable("LLM_CODEX_HOME is not an existing directory") from None
    if not stat.S_ISDIR(directory.st_mode):
        raise _unsuitable("LLM_CODEX_HOME is not an existing directory")
    if stat.S_IMODE(directory.st_mode) != _PRIVATE_DIRECTORY_MODE:
        raise _unsuitable("LLM_CODEX_HOME must have mode 0700")
    if directory.st_uid == 0:
        raise _unsuitable("LLM_CODEX_HOME is owned by root, not by the Codex worker user")
    try:
        auth = (profile / "auth.json").stat()
    except OSError:
        auth = None
    if auth is None or not stat.S_ISREG(auth.st_mode) or auth.st_size == 0:
        raise _unsuitable("LLM_CODEX_HOME has no non-empty auth.json")
    if stat.S_IMODE(auth.st_mode) != _PRIVATE_FILE_MODE:
        raise _unsuitable("LLM_CODEX_HOME auth.json must have mode 0600")
    if (auth.st_uid, auth.st_gid) != (directory.st_uid, directory.st_gid):
        raise _unsuitable("LLM_CODEX_HOME auth.json is not owned by the profile's owner")
    _require_file_store(profile / "config.toml")
    return ChildUser.switchable(directory.st_uid, directory.st_gid, "LLM_CODEX_HOME")


def _require_file_store(config_path: Path) -> None:
    try:
        config = config_path.stat()
    except OSError:
        config = None
    if config is None or not stat.S_ISREG(config.st_mode):
        raise _unsuitable("LLM_CODEX_HOME has no config.toml")
    if stat.S_IMODE(config.st_mode) != _PRIVATE_FILE_MODE:
        raise _unsuitable("LLM_CODEX_HOME config.toml must have mode 0600")
    try:
        settings = tomllib.loads(config_path.read_text())
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        raise _unsuitable("LLM_CODEX_HOME config.toml is unreadable or invalid TOML") from None
    if settings.get("cli_auth_credentials_store") != "file":
        raise _unsuitable('LLM_CODEX_HOME config.toml must set cli_auth_credentials_store = "file"')


def open_codex_profile_lock(profile: Path, owner: ChildUser) -> int:
    """A descriptor on the profile's `.codegen-codex.lock`, created owned by `owner`.

    The wrapper chmods the lock before every Codex turn, which fails on a file
    it does not own. So a lock this process creates is made under a private
    name, handed to the profile owner, and only then linked into place: no
    worker ever sees a root-owned lock inode.
    """
    lock_path = profile / CODEX_PROFILE_LOCK_NAME
    with suppress(FileNotFoundError):
        return os.open(lock_path, os.O_RDWR)
    staging = profile / f"{CODEX_PROFILE_LOCK_NAME}.{uuid.uuid4().hex}"
    descriptor = os.open(staging, os.O_RDWR | os.O_CREAT | os.O_EXCL, _PRIVATE_FILE_MODE)
    try:
        owner.own(staging)
        os.link(staging, lock_path)
    except FileExistsError:
        os.close(descriptor)
        descriptor = os.open(lock_path, os.O_RDWR)
    except BaseException:
        os.close(descriptor)
        raise
    finally:
        os.unlink(staging)
    return descriptor


class CodexTurnModel(CliTurnChatModel):
    """`codex exec` on the workers' file-backed ChatGPT profile (`LLM_CODEX_HOME`)."""

    channel: LLMChannel = LLMChannel.CODEX
    binary: str = "codex"
    codex_home: str | None = None

    def _child_user(self) -> ChildUser:
        return codex_profile_owner(self.codex_home)

    def _credentials(self) -> dict[str, str]:
        # `_child_user` has already held the profile to its rules.
        return {"CODEX_HOME": str(self.codex_home)}

    def _command(self, executable: str, workdir: Path, io_dir: Path) -> list[str]:
        schema = io_dir / "output-schema.json"
        schema.write_text(json.dumps(TURN_OUTPUT_SCHEMA))
        command = [
            executable,
            "exec",
            "--ephemeral",
            "--ignore-user-config",
            "--skip-git-repo-check",
            "--sandbox",
            "read-only",
            # config.toml is ignored above; the profile's credential store is not a choice.
            "-c",
            'cli_auth_credentials_store="file"',
            "--color",
            "never",
            "--cd",
            str(workdir),
            "--output-schema",
            str(schema),
            "--output-last-message",
            str(io_dir / "answer.json"),
        ]
        if self.model:
            command += ["--model", self.model]
        return [*command, "-"]

    def _answer(self, stdout: bytes, io_dir: Path) -> Any:
        answer = io_dir / "answer.json"
        if not answer.is_file() or not answer.read_text().strip():
            raise InvalidTurn("codex wrote no final message")
        return _json_object(answer.read_text(), "the final message")

    @asynccontextmanager
    async def _serialized(self, child: ChildUser) -> AsyncIterator[None]:
        """Hold the profile's exclusive lock; the wait counts against the call's deadline."""
        descriptor = open_codex_profile_lock(Path(str(self.codex_home)), child)
        try:
            while True:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    await asyncio.sleep(_LOCK_POLL_SECONDS)
            try:
                yield
            finally:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


class ClaudeTurnModel(CliTurnChatModel):
    """`claude -p` on a subscription OAuth token (`CLAUDE_CODE_OAUTH_TOKEN`)."""

    channel: LLMChannel = LLMChannel.CLAUDE
    binary: str = "claude"
    oauth_token: SecretStr | None = None

    def _credentials(self) -> dict[str, str]:
        token = self.oauth_token.get_secret_value() if self.oauth_token else ""
        if not token:
            raise ChannelFailure(
                ChannelFailureClass.MISSING_CREDENTIAL, "CLAUDE_CODE_OAUTH_TOKEN is not set"
            )
        return {"CLAUDE_CODE_OAUTH_TOKEN": token}

    def _child_user(self) -> ChildUser:
        if os.geteuid() != 0:
            return ChildUser(os.geteuid(), os.getegid())
        try:
            user = pwd.getpwnam(CLAUDE_CHILD_USER)
        except KeyError:
            raise _unsuitable(f"no {CLAUDE_CHILD_USER} user to run claude as") from None
        return ChildUser(user.pw_uid, user.pw_gid)

    def _settings_env(self) -> dict[str, str]:
        # Its HOME is new on every call; an updater would download itself into each one.
        return {"DISABLE_AUTOUPDATER": "1"}

    def _command(self, executable: str, workdir: Path, io_dir: Path) -> list[str]:
        command = [
            executable,
            "-p",
            "--output-format",
            "json",
            "--json-schema",
            json.dumps(TURN_OUTPUT_SCHEMA),
            "--tools",
            "",
            "--no-session-persistence",
            "--strict-mcp-config",
        ]
        if self.model:
            command += ["--model", self.model]
        return command

    @staticmethod
    def _envelope(stdout: bytes) -> Any:
        return _json_object(stdout.decode(errors="replace"), "the claude result")

    def _reported_error(self, stdout: bytes) -> str | None:
        try:
            envelope = self._envelope(stdout)
        except InvalidTurn:
            return None
        if isinstance(envelope, dict) and envelope.get("is_error"):
            return f"{envelope.get('subtype', 'error')}: {envelope.get('result', '')}"
        return None

    def _answer(self, stdout: bytes, io_dir: Path) -> Any:
        envelope = self._envelope(stdout)
        if not isinstance(envelope, dict) or envelope.get("structured_output") is None:
            raise InvalidTurn("the claude result carries no structured_output")
        return envelope["structured_output"]
