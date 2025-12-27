"""Ingest the public README into the RAG index via API webhook."""

from __future__ import annotations

import hashlib
import hmac
from http import HTTPStatus
import json
import os
from pathlib import Path
import time
from urllib import request


def _get_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"{name} is not set")
    return value


def _api_url(base_url: str, path: str) -> str:
    base = base_url.rstrip("/")
    if base.endswith("/api"):
        raise RuntimeError("API_BASE_URL must not include /api")
    return f"{base}/api/{path.lstrip('/')}"


def _hash_text(text: str) -> str:
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def main() -> None:
    api_base_url = _get_env("API_BASE_URL")
    secret = _get_env("RAG_INGEST_SECRET")

    readme_path = Path(__file__).resolve().parents[1] / "README.md"
    if not readme_path.exists():
        raise RuntimeError("README.md not found")

    content = readme_path.read_text(encoding="utf-8")
    updated_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(readme_path.stat().st_mtime))

    payload = {
        "event": "rag.docs.upsert",
        "project_id": None,
        "user_id": None,
        "repo": {
            "full_name": "codegen_orchestrator",
            "ref": "local",
            "commit_sha": "local",
        },
        "documents": [
            {
                "source_type": "public_doc",
                "source_id": "README.md",
                "source_uri": "repo://codegen_orchestrator/README.md",
                "scope": "public",
                "path": "README.md",
                "content": content,
                "language": "en",
                "updated_at": updated_at,
                "content_hash": _hash_text(content),
            }
        ],
    }

    body = json.dumps(payload, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    timestamp = str(int(time.time()))
    signature = hmac.new(
        secret.encode("utf-8"),
        f"{timestamp}.".encode() + body,
        hashlib.sha256,
    ).hexdigest()

    req = request.Request(  # noqa: S310
        _api_url(api_base_url, "rag/ingest"),
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-RAG-Timestamp": timestamp,
            "X-RAG-Signature": f"sha256={signature}",
        },
    )

    with request.urlopen(req, timeout=10) as resp:  # noqa: S310
        if resp.status != HTTPStatus.OK:
            raise RuntimeError(f"RAG ingest failed: {resp.status} {resp.read()!r}")

        print(resp.read().decode("utf-8"))


if __name__ == "__main__":
    main()
