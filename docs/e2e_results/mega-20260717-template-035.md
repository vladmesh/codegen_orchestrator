# Live mega 2026-07-17: template 0.3.5, typed env deploy и non-LLM QA

`LIVE_NO_CLEANUP=1 make test-live-mega` прошёл целиком: 7 тестов за 341.13s (05:41), exit code 0.
Это первый полный mega после удаления legacy env fallback и перехода deploy на обязательный typed
env contract. Заодно прогон подтвердил, что pin `setup-uv@v7` + uv `0.11.29` устранил зависимость
CI сгенерированного проекта от runtime-разрешения `latest` через GitHub Releases API.

## Подготовка

- `service-template` PR #51 смёржен в `cbfa6ad`, выпущен immutable tag `0.3.5`.
- В workflow шаблона и generated project используется `astral-sh/setup-uv@v7` с явным
  `version: "0.11.29"`.
- `codegen_orchestrator` PR #96 смёржен в main, production config и live harness переведены с
  шаблона `0.3.4` на `0.3.5`.
- PR #96 прошёл Required CI Gate: fast checks, service tests, integration tests и обе template
  compatibility jobs зелёные.
- Перед mega выполнен полный `make rebuild`: пересобраны service images и worker-base images,
  стек перезапущен, API стал healthy.
- Cleanup намеренно отключён на время сбора evidence через `LIVE_NO_CLEANUP=1`.

## Идентификаторы

| Ресурс | Значение |
|---|---|
| Project | `b09d42dc-d106-487f-8b88-a7b27dab73c0` |
| Project name | `live-test-47e95570` |
| Repository | `project-factory-organization/live-test-47e95570` |
| Repository id | `repo-c3931d60`, provider id `1303403147` |
| Story | `story-a6ab0590` |
| Engineering task | `task-1bcc564c` |
| Engineering run | `eng-f79342acfa2e` |
| Deploy run | `deploy-poll-b94e8ab4` |
| GitHub deploy Actions run | `29546421874` |
| QA run | `qa-72d8417e` |
| Server | `vps-273978`, `185.81.166.84` |
| Application | `19` |
| Backend allocation | `8002`, allocation `35` |
| Manifest | `.live-manifests/b09d42dc-d106-487f-8b88-a7b27dab73c0.json` |

## Таймлайн

Время в API хранится в UTC.

| Время | Событие |
|---|---|
| 00:59:09 | Созданы project и repository, scaffold получил template `0.3.5`. |
| 00:59:45 | Созданы story и noop engineering task. |
| 01:00:03 | Создан engineering run. |
| 01:00:04 | Engineering run стартовал, application получила backend allocation `8002`. |
| 01:01:02 | Engineering task завершена, итоговый commit `e72ef06235dddb9ae6f0778688d8eea07f946910`. |
| 01:02:45 | Создан deploy run, GitHub Actions deploy run `29546421874`. |
| 01:02:49 | Project перешёл в `active`. |
| 01:04:46 | Создан non-LLM QA run, `/health` вернул 200. |
| 01:05:16 | Story завершена, PR #1 привязан к story. |

## Результаты mega

| Тест | Результат | Что доказано |
|---|---|---|
| `test_project_active` | PASSED | Полный lifecycle довёл project до `active`. |
| `test_env_contract_committed_by_scaffold` | PASSED | Scaffold закоммитил канонический env-contract. |
| `test_env_contract_present_on_merged_sha` | PASSED | Контракт сохранился на SHA после merge, deploy не читает эфемерный PR ref. |
| `test_deploy_run_outcome_success` | PASSED | Typed deploy завершился outcome `success`. |
| `test_port_allocated` | PASSED | Backend получил owned allocation и опубликованный web-порт. |
| `test_health_endpoint` | PASSED | Внешний `GET /health` доступен на `185.81.166.84:8002`. |
| `test_non_llm_qa_passed` | PASSED | Детерминированный post-deploy QA проверил acceptance criterion без LLM. |

## Scaffold и template pin

Project config зафиксировал:

```text
source: gh:vladmesh/service-template
requested_ref: 0.3.5
commit: 0.3.5
modules: [backend]
agent_type: noop
```

Сгенерированное дерево содержит `.framework/framework/toolchain.py`, typed env-contract fragments,
generated CI, backend specs и generated router registry. Падение `Set up uv`, которое блокировало
предыдущие mega, не воспроизвелось: CI дошёл до merge и post-merge deploy.

## Typed env contract и deploy

- Legacy analyzer/LLM fallback не использовался, env contract обязателен.
- Контракт был создан scaffold, присутствовал на merged SHA и успешно загружен deploy.
- Deploy вернул `deploy_outcome=success`, `application_id=19` и
  `deployed_url=http://185.81.166.84:8002`.
- GitHub deploy workflow завершился успешно, Actions run `29546421874`.
- Smoke result: backend `pass`, HTTP 200.
- Project содержит сохранённые generated secrets, но их имена и значения в отчёте не раскрываются.

## Deploy и health evidence

Application `19` после прогона:

```text
status: running
server: vps-273978
backend: 185.81.166.84:8002
last_health_check: 2026-07-17T01:09:43Z
response_time_ms: 19
uptime_pct_24h: 100.0
```

Повторный внешний probe после теста: `GET http://185.81.166.84:8002/health` вернул HTTP 200 за
примерно 2.3ms. Инфраструктурные allocations для application: postgres `8003`, redis `8004`;
наружу mega проверяет backend allocation `8002`.

## Non-LLM QA

QA run `qa-72d8417e` завершился `qa_outcome=passed`:

```text
acceptance criterion: GET /health returns 200
report: GET /health returns 200: got 200
failed_checks: []
deployed_url: http://185.81.166.84:8002
```

Это подтверждает новый QA-контракт: acceptance criteria дошли до post-deploy QA, HTTP-only проверка
не создавала agent scaffolding и не требовала LLM.

## Что этот прогон закрыл

- GitHub REST API восстановился достаточно для checkout, paths-filter, CI и deploy workflow.
- Pin uv убрал Releases API из критического пути generated CI.
- Template `0.3.5` реально используется scaffold, а не только записан в system config.
- Typed env artifact доступен на merged SHA.
- Typed deploy resolution проходит без legacy fallback.
- Post-merge deploy run находится и проверяется mega harness.
- Acceptance criteria передаются в non-LLM QA, QA завершает story.

## Неблокирующие наблюдения

- Dockerfile worker-base самого оркестратора всё ещё использует
  `COPY --from=ghcr.io/astral-sh/uv:latest`; это registry tag, не Releases API path из карточки 619.
- Manifest явно владеет backend allocation `35`; postgres/redis allocations `36`/`37` созданы
  deploy и должны исчезнуть каскадно при удалении application/project. Cleanup ниже проверяет итог.
- Manual PR #95 с удалением мёртвого `AllocationDTO` зелёный, но после merge PR #96 требует update
  branch; к mega и template pin он отношения не имеет.

## Cleanup

Первый `make test-live-clean` остановился fail-closed на registry repository. Registry вернул 404
для manifest, который ещё присутствовал в stale tag list; cleanup делал безусловный
`raise_for_status()` и ошибочно считал уже отсутствующий artifact фатальной ошибкой.

Cleanup исправлен локально:

- 404 при чтении manifest по tag трактуется как доказанное отсутствие;
- финальная проверка различает stale tag names и реально доступные manifests;
- добавлен regression test со stale tag и двумя последовательными 404;
- `3 passed, 67 deselected`, Ruff и `git diff --check` зелёные.

После исправления повторный `make test-live-clean` прошёл с exit code 0:

```text
Owned capability stream entries removed and verified.
Database cleaned.
No local test containers found.
Orphaned workspaces removed: 3.
Verifying absence of live-test residue.
Live test cleanup fully complete.
```

Постпроверка:

- manifest `b09d42dc-d106-487f-8b88-a7b27dab73c0.json` удалён;
- `GET /api/projects/b09d42dc-d106-487f-8b88-a7b27dab73c0` возвращает 404;
- applications с именами `live-test-*` отсутствуют, allocations удалены каскадно;
- owned Redis capability entries удалены и проверены cleanup-скриптом;
- workspace `repo-c3931d60` удалён вместе с двумя stale live workspaces;
- GitHub repository и registry manifests удалены manifest recovery до глобального sweep.

Итог: mega зелёная, оставленных owned live-test ресурсов нет.
