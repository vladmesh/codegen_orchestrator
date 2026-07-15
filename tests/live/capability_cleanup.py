"""Fail-closed cleanup of live-test capability stream entries."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import json

from live_harness import CleanupError

from shared.queues import DEPLOY_QUEUE, ENGINEERING_QUEUE, QA_GROUP, QA_QUEUE, WORKER_GROUP


@dataclass(frozen=True)
class CapabilityMessage:
    """One project-owned capability entry and every group that has it pending."""

    stream: str
    message_id: str
    groups: tuple[str, ...]


_CAPABILITY_GROUPS = {
    ENGINEERING_QUEUE: (WORKER_GROUP,),
    DEPLOY_QUEUE: (WORKER_GROUP,),
    QA_QUEUE: (QA_GROUP,),
}


def _find_script() -> str:
    """Return Lua that finds owned entries in streams and their relevant PELs."""
    return """
local function owned(fields)
  local payload = fields['data']
  if payload then
    local ok, decoded = pcall(cjson.decode, payload)
    if ok and type(decoded) == 'table' then fields = decoded end
  end
  if fields['project_id'] == ARGV[1] then return true end
  for _, identifier in ipairs(cjson.decode(ARGV[2])) do
    if fields['task_id'] == identifier or fields['run_id'] == identifier
      or fields['story_id'] == identifier then return true end
  end
  return false
end
local found = {}
for stream_index, stream in ipairs(KEYS) do
  local groups = cjson.decode(ARGV[2 + stream_index])
  local pending_groups = {}
  local ids = {}
  for _, item in ipairs(redis.call('XRANGE', stream, '-', '+')) do
    if owned(item[2]) then ids[item[1]] = true end
  end
  for _, group in ipairs(groups) do
    local pending_ok, pending = pcall(redis.call, 'XPENDING', stream, group, '-', '+', 1000)
    if pending_ok then
      table.insert(pending_groups, group)
      for _, pending_item in ipairs(pending) do
        local entry = redis.call('XRANGE', stream, pending_item[1], pending_item[1])
        if #entry > 0 and owned(entry[1][2]) then ids[pending_item[1]] = true end
      end
    end
  end
  for id, _ in pairs(ids) do
    table.insert(found, {stream=stream, id=id, groups=pending_groups})
  end
end
return cjson.encode(found)
"""


def find_owned_capability_messages(
    project_id: str,
    identifiers: set[str],
    *,
    command: Callable[..., str],
) -> list[CapabilityMessage]:
    """Find project-owned queued and pending capability messages without mutating streams."""
    streams = tuple(_CAPABILITY_GROUPS)
    encoded_groups = [json.dumps(_CAPABILITY_GROUPS[stream]) for stream in streams]
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
        )
        for entry in entries
    ]


def cleanup_owned_capability_messages(
    project_id: str,
    identifiers: set[str],
    *,
    command: Callable[..., str],
    on_discovered: Callable[[CapabilityMessage], None] | None = None,
) -> list[CapabilityMessage]:
    """ACK and delete owned entries, then prove no owned queue or PEL residue remains."""
    discovered = find_owned_capability_messages(project_id, identifiers, command=command)
    for message in discovered:
        if on_discovered:
            on_discovered(message)
        for group in message.groups:
            command("XACK", message.stream, group, message.message_id)
        command("XDEL", message.stream, message.message_id)
    residue = find_owned_capability_messages(project_id, identifiers, command=command)
    if residue:
        details = ", ".join(f"{entry.stream}/{entry.message_id}" for entry in residue)
        raise CleanupError(f"capability stream residue remains: {details}")
    return []
