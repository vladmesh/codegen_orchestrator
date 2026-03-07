#!/usr/bin/env python3
"""Enrich existing tasks with descriptions and fix statuses.

Usage:
    python scripts/enrich_tasks.py [--api-url http://localhost:8000] [--dry-run]
"""

import argparse
from http import HTTPStatus

import httpx

# Descriptions for Done items (from CHANGELOG and backlog history)
ENRICHMENTS: dict[str, dict] = {
    "#51": {
        "description": (
            "Секреты не сохранялись: `POST /projects/{id}/config/secrets` возвращал 200, "
            "но никогда не записывал. Причина: plain `JSON` column не детектил "
            "in-place dict mutations. "
            "Фикс: `MutableDict.as_mutable(JSON)` на `Project.config` и `project_spec`. "
            "Также: deploy-worker не сбрасывал статус проекта при `missing_user_secrets` — "
            "теперь откатывает в `failed`."
        ),
        "type": "fix",
    },
    "#50": {
        "description": (
            "Описание проекта терялось в create flow. `trigger_engineering` не передавал "
            "`detailed_spec` в project config для `action=create`. "
            "Фикс: PATCH `detailed_spec` + fallback на `feature_description` из queue."
        ),
        "type": "fix",
    },
    "#49": {
        "description": (
            "Inline keyboard кнопка 'Add User' для админов в Telegram боте. "
            "Создаёт пользователей через `POST /users/` по text input flow."
        ),
        "type": "feature",
    },
    "#48": {
        "description": (
            "PO consumer auto-repairs orphan tool_calls в checkpoint — "
            "сломанные tool_calls блокировали пользователей навсегда. "
            "Также: ruff per-file-ignores для `**/tests/**` paths."
        ),
        "type": "fix",
    },
    "#47": {
        "description": (
            "Race condition когда LLM вызывал `set_project_secret` параллельно — "
            "секреты терялись. Фикс: `POST /api/projects/{id}/config/secrets` "
            "атомарный merge с `SELECT FOR UPDATE`."
        ),
        "type": "fix",
    },
    "#42": {
        "description": (
            "Integration test `test_post_projects_pure_db` падал — "
            "не хватало `X-Telegram-ID` header и seed user через API."
        ),
        "type": "fix",
    },
    "#45": {
        "description": (
            "PO получает контекст пользователя (user_id, user_name). "
            "`hint` параметр на `set_project_secret` — hints в `config.env_hints`. "
            "DeveloperNode инжектит `## Provided Environment Variables` в TASK.md."
        ),
        "type": "feature",
    },
    "#44": {
        "description": (
            "PO `web_search` tool: DuckDuckGo поиск для документации "
            "сторонних API. System prompt guidance когда использовать поиск."
        ),
        "type": "feature",
    },
    "#43": {
        "description": (
            "PO ведёт сократический диалог: собирает требования перед "
            "запуском инженерного этапа. Промпт фокусируется на продуктовых "
            "вопросах для нетехнических пользователей."
        ),
        "type": "feature",
    },
    "#8": {
        "description": (
            "Workspace failure counter в Redis: отслеживает consecutive failures per project. "
            "Force wipe после 2 failures, circuit breaker после 3 (auto-unblock TTL 48h). "
            "`reason` field на `DeleteWorkerCommand`."
        ),
        "type": "feature",
    },
    "#55": {
        "complete": True,  # Mark as done
    },
}


TRANSITIONS_TO_DONE = {
    "in_dev": ["testing", "done"],
    "in_review": ["testing", "done"],
    "testing": ["done"],
    "backlog": ["todo", "in_dev", "testing", "done"],
    "todo": ["in_dev", "testing", "done"],
    "failed": ["backlog", "todo", "in_dev", "testing", "done"],
}


def _path_to_done(status: str) -> list[str]:
    """Return list of transitions needed to reach done from current status."""
    return TRANSITIONS_TO_DONE.get(status, [])


def enrich(api_url: str, dry_run: bool = False) -> None:
    """Enrich tasks via API."""
    # Fetch all items
    resp = httpx.get(f"{api_url}/api/tasks/", params={"limit": 100}, timeout=10)
    resp.raise_for_status()
    items = resp.json()

    # Build lookup: title prefix (#NN) -> item
    lookup: dict[str, dict] = {}
    for item in items:
        # Extract #NN from title
        title = item["title"]
        if title.startswith("#"):
            tag = title.split(" ")[0]  # "#51"
            lookup[tag] = item

    updated = 0
    for tag, enrichment in ENRICHMENTS.items():
        if tag not in lookup:
            print(f"  SKIP {tag} — not found in DB")
            continue

        item = lookup[tag]
        item_id = item["id"]

        # Update description and type
        if "description" in enrichment:
            patch = {"description": enrichment["description"]}
            if "type" in enrichment:
                patch["type"] = enrichment["type"]

            print(f"  PATCH {tag} {item['title'][:50]}")
            if not dry_run:
                r = httpx.patch(
                    f"{api_url}/api/tasks/{item_id}",
                    json=patch,
                    timeout=10,
                )
                if r.status_code == HTTPStatus.OK:
                    print("    -> updated")
                    updated += 1
                else:
                    print(f"    -> FAILED: {r.status_code} {r.text}")

        # Complete item if requested (may need multi-step: in_dev -> testing -> done)
        if enrichment.get("complete") and item["status"] != "done":
            print(f"  COMPLETE {tag} {item['title'][:50]} (was {item['status']})")
            if not dry_run:
                # Walk through transitions to reach done
                path = _path_to_done(item["status"])
                ok = True
                for step in path:
                    r = httpx.post(
                        f"{api_url}/api/tasks/{item_id}/transition",
                        params={"to_status": step},
                        json={"actor": "migration"},
                        timeout=10,
                    )
                    if r.status_code == HTTPStatus.OK:
                        print(f"    -> {step}")
                    else:
                        print(f"    -> FAILED at {step}: {r.status_code} {r.text}")
                        ok = False
                        break
                if ok:
                    updated += 1

    print(f"\nUpdated {updated} items" + (" (dry run)" if dry_run else ""))


def main():
    parser = argparse.ArgumentParser(description="Enrich tasks with descriptions")
    parser.add_argument("--api-url", default="http://localhost:8000")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    enrich(args.api_url, args.dry_run)


if __name__ == "__main__":
    main()
