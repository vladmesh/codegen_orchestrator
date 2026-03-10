# Debug: engineering
**Time**: 2026-03-10T21:20:37.946315+00:00

## Context
- project_id: `94195d1f-2640-4b27-9434-2b579ee90fd6`
- project_name: `live-test-539927e0`
- scaffold_status: `scaffolded`
- task_id: `task-f9df8508`
- task_status: `todo`
- story_status: `in_progress`
- final_project_status: `None`
- deployed_url: `None`
- engineering_elapsed: `420`

## scaffolder logs (last 30)
```
135[0m [36mproject_id[0m=[35m94195d1f-2640-4b27-9434-2b579ee90fd6[0m [36mrepository_id[0m=[35mrepo-614903d6[0m [36mservice[0m=[35mscaffolder[0m
scaffolder-1  | [2m2026-03-10T21:13:35.092111[0m [[32m[1minfo     [0m] [1mscaffold_complete             [0m [[0m[1m[34msrc.scaffold[0m][0m [36mfunc_name[0m=[35mrun_scaffold[0m [36mlineno[0m=[35m162[0m [36mproject_id[0m=[35m94195d1f-2640-4b27-9434-2b579ee90fd6[0m [36mrepository_id[0m=[35mrepo-614903d6[0m [36mservice[0m=[35mscaffolder[0m [36mtree_lines[0m=[35m90[0m
scaffolder-1  | HTTP Request: GET http://api:8000/api/projects/94195d1f-2640-4b27-9434-2b579ee90fd6 "HTTP/1.1 200 OK"
scaffolder-1  | HTTP Request: PATCH http://api:8000/api/projects/94195d1f-2640-4b27-9434-2b579ee90fd6 "HTTP/1.1 200 OK"
scaffolder-1  | [2m2026-03-10T21:13:35.124339[0m [[32m[1minfo     [0m] [1mproject_config_updated        [0m [[0m[1m[34msrc.clients.api[0m][0m [36mfunc_name[0m=[35mupdate_project_config[0m [36mlineno[0m=[35m71[0m [36mproject_id[0m=[35m94195d1f-2640-4b27-9434-2b579ee90fd6[0m [36mservice[0m=[35mscaffolder[0m
scaffolder-1  | HTTP Request: PATCH http://api:8000/api/projects/94195d1f-2640-4b27-9434-2b579ee90fd6 "HTTP/1.1 200 OK"
scaffolder-1  | [2m2026-03-10T21:13:35.139674[0m [[32m[1minfo     [0m] [1mproject_status_updated        [0m [[0m[1m[34msrc.clients.api[0m][0m [36mfunc_name[0m=[35mupdate_project_status[0m [36mlineno[0m=[35m59[0m [36mproject_id[0m=[35m94195d1f-2640-4b27-9434-2b579ee90fd6[0m [36mservice[0m=[35mscaffolder[0m [36mstatus[0m=[35mscaffolded[0m
scaffolder-1  | [2m2026-03-10T21:13:35.139873[0m [[32m[1minfo     [0m] [1mscaffold_job_success          [0m [[0m[1m[34msrc.consumer[0m][0m [36mfunc_name[0m=[35mprocess_scaffold_job[0m [36mlineno[0m=[35m132[0m [36mproject_id[0m=[35m94195d1f-2640-4b27-9434-2b579ee90fd6[0m [36mrepository_id[0m=[35mrepo-614903d6[0m [36mservice[0m=[35mscaffolder[0m
```

## engineering-worker logs (last 30)
```
-4a9114e5[0m [36mservice[0m=[35mengineering-worker[0m
engineering-worker-1  | HTTP Request: POST http://api:8000/api/tasks/task-4a9114e5/transition?to_status=testing "HTTP/1.1 200 OK"
engineering-worker-1  | [2m2026-03-10T21:12:46.181212[0m [[32m[1minfo     [0m] [1mtask_status_updated           [0m [[0m[1m[34m__main__[0m][0m [36mfunc_name[0m=[35m_update_task_status[0m [36mlineno[0m=[35m60[0m [36mnew_status[0m=[35mtesting[0m [36mplanning_task_id[0m=[35mtask-4a9114e5[0m [36mservice[0m=[35mengineering-worker[0m
engineering-worker-1  | HTTP Request: POST http://api:8000/api/tasks/task-4a9114e5/transition?to_status=done "HTTP/1.1 200 OK"
engineering-worker-1  | [2m2026-03-10T21:12:46.193174[0m [[32m[1minfo     [0m] [1mtask_status_updated           [0m [[0m[1m[34m__main__[0m][0m [36mfunc_name[0m=[35m_update_task_status[0m [36mlineno[0m=[35m60[0m [36mnew_status[0m=[35mdone[0m [36mplanning_task_id[0m=[35mtask-4a9114e5[0m [36mservice[0m=[35mengineering-worker[0m
engineering-worker-1  | HTTP Request: POST http://api:8000/api/tasks/task-4a9114e5/events "HTTP/1.1 201 Created"
engineering-worker-1  | [2m2026-03-10T21:12:46.203490[0m [[32m[1minfo     [0m] [1mdeploy_decision               [0m [[0m[1m[34m__main__[0m][0m [36meffective_skip_deploy[0m=[35mTrue[0m [36mfunc_name[0m=[35m_handle_engineering_success[0m [36mlineno[0m=[35m663[0m [36mplanning_task_id[0m=[35mtask-4a9114e5[0m [36mservice[0m=[35mengineering-worker[0m [36mskip_deploy[0m=[35mTrue[0m [36mtask_id[0m=[35meng-3b5fb6bab69d[0m
engineering-worker-1  | [2m2026-03-10T21:12:46.203729[0m [[32m[1minfo     [0m] [1mdeploy_skipped                [0m [[0m[1m[34m__main__[0m][0m [36mfunc_name[0m=[35m_handle_engineering_success[0m [36mlineno[0m=[35m744[0m [36mproject_id[0m=[35mcdbdb413-016e-41f5-ba3d-5c8e8c22b726[0m [36mservice[0m=[35mengineering-worker[0m [36mtask_id[0m=[35meng-3b5fb6bab69d[0m
```

## scheduler logs (last 30)
```
12/v/enum
scheduler-1  | Traceback (most recent call last):
scheduler-1  |   File "/app/src/tasks/task_dispatcher.py", line 523, in task_dispatcher_loop
scheduler-1  |     scaffolds = await trigger_scaffolds(api_client, redis_client)
scheduler-1  |                 ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
scheduler-1  |   File "/app/src/tasks/scaffold_trigger.py", line 41, in trigger_scaffolds
scheduler-1  |     projects = await api_client.get_projects()
scheduler-1  |                ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
scheduler-1  |   File "/app/src/clients/api.py", line 51, in get_projects
scheduler-1  |     return [ProjectDTO.model_validate(p) for p in resp.json()]
scheduler-1  |             ^^^^^^^^^^^^^^^^^^^^^^^^^^^^
scheduler-1  |   File "/usr/local/lib/python3.12/site-packages/pydantic/main.py", line 716, in model_validate
scheduler-1  |     return cls.__pydantic_validator__.validate_python(
scheduler-1  |            ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
scheduler-1  | pydantic_core._pydantic_core.ValidationError: 1 validation error for ProjectDTO
scheduler-1  | status
scheduler-1  |   Input should be 'draft', 'scaffolding', 'scaffolded', 'scaffold_failed', 'developing', 'testing', 'deploying', 'active', 'maintenance', 'failed', 'missing' or 'archived' [type=enum, input_value='error', input_type=str]
scheduler-1  |     For further information visit https://errors.pydantic.dev/2.12/v/enum
scheduler-1  | [2m2026-03-10T21:20:37.461853[0m [[32m[1minfo     [0m] [1mgithub_sync_start             [0m [[0m[1m[34msrc.tasks.github_sync[0m][0m [36mfunc_name[0m=[35msync_projects_worker[0m [36mlineno[0m=[35m332[0m [36morg_name[0m=[35mproject-factory-organization[0m [36mservice[0m=[35mscheduler[0m
scheduler-1  | HTTP Request: GET https://api.github.com/orgs/project-factory-organization/installation "HTTP/1.1 200 OK"
scheduler-1  | HTTP Request: POST https://api.github.com/app/installations/100979986/access_tokens "HTTP/1.1 201 Created"
```

## deploy-worker logs (last 30)
```
: Client error '404 Not Found' for url 'https://api.github.com/repos/project-factory-organization/live-test-f7010201/installation'
deploy-worker-1  | For more information check: https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/404
deploy-worker-1  | HTTP Request: PATCH http://api:8000/api/projects/cdbdb413-016e-41f5-ba3d-5c8e8c22b726 "HTTP/1.1 200 OK"
deploy-worker-1  | [2m2026-03-10T21:13:09.225924[0m [[32m[1minfo     [0m] [1mdevops_subgraph_result        [0m [[0m[1m[34m__main__[0m][0m [36mdeployed_url[0m=[35mNone[0m [36merrors[0m=[35m["Deployment error: Client error '404 Not Found' for url 'https://api.github.com/repos/project-factory-organization/live-test-f7010201/installation'\nFor more information check: https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/404"][0m [36mfunc_name[0m=[35mprocess_deploy_job[0m [36mhas_smoke_result[0m=[35mTrue[0m [36mlineno[0m=[35m412[0m [36mresult_keys[0m=[35m['allocated_resources', 'deployed_url', 'deployment_result', 'env_analysis', 'env_variables', 'errors', 'messages', 'missing_user_secrets', 'project_id', 'project_spec', 'provided_secrets', 'repo_info', 'resolved_secrets', 'smoke_result'][0m [36mservice[0m=[35mdeploy-worker[0m [36msmoke_result[0m=[35mNone[0m [36mtask_id[0m=[35mdeploy-e59ac2134dfb[0m
deploy-worker-1  | [2m2026-03-10T21:13:09.226137[0m [[31m[1merror    [0m] [1mdeploy_job_failed             [0m [[0m[1m[34m__main__[0m][0m [36merrors[0m=[35m["Deployment error: Client error '404 Not Found' for url 'https://api.github.com/repos/project-factory-organization/live-test-f7010201/installation'\nFor more information check: https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/404"][0m [36mfunc_name[0m=[35mprocess_deploy_job[0m [36mlineno[0m=[35m489[0m [36mservice[0m=[35mdeploy-worker[0m [36mtask_id[0m=[35mdeploy-e59ac2134dfb[0m
deploy-worker-1  | HTTP Request: PATCH http://api:8000/api/runs/deploy-e59ac2134dfb "HTTP/1.1 200 OK"
```
