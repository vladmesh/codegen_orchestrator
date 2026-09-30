"""Fail-closed cleanup of live-test capability stream entries."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
import json
import time

from live_harness import CleanupError
import structlog

from shared.queues import DEPLOY_QUEUE, ENGINEERING_QUEUE, QA_GROUP, QA_QUEUE, WORKER_GROUP


@dataclass(frozen=True)
class CapabilityMessage:
    """One project-owned capability entry and every group that has it pending."""

    stream: str
    message_id: str
    groups: tuple[str, ...]
    project_id: str | None = None
    run_id: str | None = None
    task_id: str | None = None


logger = structlog.get_logger()

# The platform keeps publishing legitimately while teardown runs (the scheduler's
# temporary-access revoke lands on deploy:queue after the run settles), so cleanup
# rescans until a scan is clean: at most SETTLE_ROUNDS residue scans, with a pause
# after each non-clean one, so owned entries still appearing after ~10 s fail closed.
SETTLE_ROUNDS = 6
SETTLE_PAUSE_SECONDS = 2.0

_CAPABILITY_GROUPS = {
    ENGINEERING_QUEUE: (WORKER_GROUP,),
    DEPLOY_QUEUE: (WORKER_GROUP,),
    QA_QUEUE: (QA_GROUP,),
}


def _find_script() -> str:
    """Return Lua that finds owned entries in streams and their relevant PELs."""
    return """
local function field_map(values)
  local fields = {}
  for index = 1, #values, 2 do fields[values[index]] = values[index + 1] end
  return fields
end
local function payload_fields(values)
  local fields = field_map(values)
  local payload = fields['data']
  if payload then
    local ok, decoded = pcall(cjson.decode, payload)
    if ok and type(decoded) == 'table' then fields = decoded end
  end
  return fields
end
local function owned(values)
  local fields = payload_fields(values)
  if fields['project_id'] == ARGV[1] then return true end
  for _, identifier in ipairs(cjson.decode(ARGV[2])) do
    if fields['task_id'] == identifier or fields['run_id'] == identifier
      or fields['story_id'] == identifier then return true end
  end
  return false
end
local function carried(values)
  local fields = payload_fields(values)
  return {project_id=fields['project_id'], run_id=fields['run_id'], task_id=fields['task_id']}
end
local found = {}
for stream_index, stream in ipairs(KEYS) do
  local groups = cjson.decode(ARGV[2 + stream_index])
  local pending_groups = {}
  local ids = {}
  for _, item in ipairs(redis.call('XRANGE', stream, '-', '+')) do
    if owned(item[2]) then ids[item[1]] = carried(item[2]) end
  end
  for _, group in ipairs(groups) do
    local start = '-'
    local group_exists = false
    while true do
      local pending_ok, pending = pcall(redis.call, 'XPENDING', stream, group, start, '+', 1000)
      if not pending_ok then break end
      group_exists = true
      for _, pending_item in ipairs(pending) do
        local entry = redis.call('XRANGE', stream, pending_item[1], pending_item[1])
        if #entry > 0 and owned(entry[1][2]) then ids[pending_item[1]] = carried(entry[1][2]) end
      end
      if #pending < 1000 then break end
      start = '(' .. pending[#pending][1]
    end
    if group_exists then table.insert(pending_groups, group) end
  end
  for id, fields in pairs(ids) do
    table.insert(found, {stream=stream, id=id, groups=pending_groups, project_id=fields.project_id,
      run_id=fields.run_id, task_id=fields.task_id})
  end
end
if next(found) == nil then return '[]' end
return cjson.encode(found)
"""


def find_owned_capability_messages(
    project_id: str,
    identifiers: set[str],
    *,
    command: Callable[..., str],
    bindings: Mapping[str, tuple[str, ...]] = _CAPABILITY_GROUPS,
) -> list[CapabilityMessage]:
    """Find project-owned queued and pending capability messages without mutating streams."""
    streams = tuple(bindings)
    encoded_groups = [json.dumps(bindings[stream]) for stream in streams]
    raw = command(
        "EVAL",
        _find_script(),
        str(len(streams)),
        *streams,
        project_id,
        json.dumps(sorted(identifiers)),
        *encoded_groups,
    )
    try:
        entries = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CleanupError("could not inspect capability stream ownership") from exc
    if not isinstance(entries, list):
        raise CleanupError("could not inspect capability stream ownership")
    return [
        CapabilityMessage(
            stream=entry["stream"],
            message_id=entry["id"],
            groups=tuple(entry["groups"]),
            project_id=_carried(entry.get("project_id")),
            run_id=_carried(entry.get("run_id")),
            task_id=_carried(entry.get("task_id")),
        )
        for entry in entries
    ]


def _carried(value: object) -> str | None:
    """Return an identifier an entry carried, as text for logs and evidence."""
    return None if value is None else str(value)


def cleanup_owned_capability_messages(
    project_id: str,
    identifiers: set[str],
    *,
    command: Callable[..., str],
    on_discovered: Callable[[CapabilityMessage], None] | None = None,
    bindings: Mapping[str, tuple[str, ...]] = _CAPABILITY_GROUPS,
    sleep: Callable[[float], None] = time.sleep,
) -> list[CapabilityMessage]:
    """ACK and delete owned entries until a scan proves no owned queue or PEL residue remains.

    An owned entry the platform publishes while teardown runs is cleaned in a later
    round, logged and reported through ``on_discovered`` like the first round's; only
    entries still appearing after the settle budget raise ``CleanupError``.
    """

    def settle(messages: list[CapabilityMessage]) -> None:
        for message in messages:
            if on_discovered:
                on_discovered(message)
            for group in message.groups:
                command("XACK", message.stream, group, message.message_id)
            command("XDEL", message.stream, message.message_id)

    settle(
        find_owned_capability_messages(project_id, identifiers, command=command, bindings=bindings)
    )
    for settle_round in range(1, SETTLE_ROUNDS):
        residue = find_owned_capability_messages(
            project_id, identifiers, command=command, bindings=bindings
        )
        if not residue:
            return []
        for message in residue:
            logger.warning(
                "live_capability_message_published_during_cleanup",
                stream=message.stream,
                message_id=message.message_id,
                project_id=message.project_id,
                run_id=message.run_id,
                task_id=message.task_id,
                settle_round=settle_round,
            )
        settle(residue)
        sleep(SETTLE_PAUSE_SECONDS)
    residue = find_owned_capability_messages(
        project_id, identifiers, command=command, bindings=bindings
    )
    if residue:
        details = ", ".join(f"{entry.stream}/{entry.message_id}" for entry in residue)
        raise CleanupError(f"capability stream residue remains: {details}")
    return []
