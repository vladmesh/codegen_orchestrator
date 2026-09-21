"""Whether a level-1 story merge contains only its declared product change."""

from __future__ import annotations

from collections.abc import Iterable

from worker_wrapper.injected_paths import offending_paths

MAKEFILE_OVERRIDE_MARKER = "# --- orchestrator overrides ---"


def change_set_comparison(observation: dict | None, *, expected_paths: Iterable[str]) -> dict:
    """State whether the merge is exactly its declared change set without judging it red."""
    paths = observation.get("changed_paths") if isinstance(observation, dict) else None
    if not isinstance(paths, list) or not all(isinstance(path, str) and path for path in paths):
        return {"exact_match": None, "extra_paths": [], "missing_declared_paths": []}
    expected = set(expected_paths)
    actual = set(paths)
    return {
        "exact_match": actual == expected and len(paths) == len(actual),
        "extra_paths": sorted(actual - expected),
        "missing_declared_paths": sorted(expected - actual),
    }


def merge_artifact_mismatches(
    observation: dict | None,
    *,
    product_agents_content: str | None,
) -> list[str]:
    """Return why one merged file set is not the level-1 story's product change.

    The GitHub commit API reports the paths a merge changed from its parent.  The
    level-1 change set declares the exact set it is allowed to change, which is
    stronger than merely proving the worker wrapper's injected paths are absent.
    The two product files the orchestrator used to edit need their content read
    too: a path alone cannot distinguish a legitimate product Makefile change
    from the old compose proxy, nor tell that product instructions were replaced.
    """
    if not isinstance(observation, dict):
        return ["the story merge file set was never captured"]

    reasons: list[str] = []
    if not observation.get("merge_commit_sha"):
        reasons.append("the merge file set names no merge commit")
    if not observation.get("default_branch"):
        reasons.append("the merge file set names no product default branch")
    if observation.get("merged_into_default_branch") is not True:
        reasons.append("the recorded merge commit is not contained in the product default branch")

    paths = observation.get("changed_paths")
    if not isinstance(paths, list) or not all(isinstance(path, str) and path for path in paths):
        reasons.append("the merge file set carries no readable changed paths")
        return reasons

    if len(paths) != len(set(paths)):
        reasons.append("the merge file set repeats a changed path")

    injected = offending_paths(paths)
    if injected:
        reasons.append(f"the merge changes injected path(s): {', '.join(injected)}")

    contents = observation.get("file_contents")
    if not isinstance(contents, dict):
        contents = {}
    if "Makefile" in paths:
        makefile = contents.get("Makefile")
        if not isinstance(makefile, str):
            reasons.append("the merged Makefile content was not captured")
        elif MAKEFILE_OVERRIDE_MARKER in makefile:
            reasons.append("the merged Makefile carries the orchestrator overrides block")

    if "AGENTS.md" in paths:
        agents = contents.get("AGENTS.md")
        if not isinstance(agents, str):
            reasons.append("the merged AGENTS.md content was not captured")
        elif not isinstance(product_agents_content, str):
            reasons.append("the product AGENTS.md content before the merge was not captured")
        elif agents != product_agents_content:
            reasons.append("the merged AGENTS.md overwrites the product instructions")
    return reasons
