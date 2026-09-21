"""Tests for preserving the last distinct worker-image release record."""

from __future__ import annotations

import json

from scripts.rotate_worker_image_records import rotate_previous_record


def _record(source_hash: str) -> dict:
    return {
        "source_hash": source_hash,
        "images": {
            name: {"reference": f"{name}@sha256:{source_hash}"}
            for name in (
                "worker-base-common",
                "worker-base-claude",
                "worker-base-factory",
                "worker-base-codex",
            )
        },
    }


def test_same_source_redeploy_keeps_the_older_distinct_generation(tmp_path):
    current = tmp_path / "current.json"
    previous = tmp_path / "previous.json"
    current.write_text(json.dumps(_record("current")))
    previous.write_text(json.dumps(_record("older")))

    rotate_previous_record(current, previous, "current")

    assert json.loads(previous.read_text())["source_hash"] == "older"


def test_new_source_rotates_current_record_to_previous(tmp_path):
    current = tmp_path / "current.json"
    previous = tmp_path / "previous.json"
    current.write_text(json.dumps(_record("current")))
    previous.write_text(json.dumps(_record("older")))

    rotate_previous_record(current, previous, "next")

    assert json.loads(previous.read_text())["source_hash"] == "current"
