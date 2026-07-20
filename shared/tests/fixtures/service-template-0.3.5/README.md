Verbatim rendered fixture from service-template tag `0.3.5` (`cbfa6ad4`),
generated with backend, tg_bot, notifications, and frontend modules. It keeps
the generated-project paths and includes Compose files, workflows, entrypoints,
settings, and environment contract fragments used by the gate.

The tag must match `scheduler.service_template_ref` in
`scripts/system_configs.yaml`; `test_env_usage.py` fails when they drift, because
a fixture from an unpinned tag stops describing what deploys actually read.

Regenerate with:

    copier copy gh:vladmesh/service-template <out> --defaults --overwrite \
      --vcs-ref=<pinned-ref> \
      --data project_name=env_fixture \
      --data modules=backend,tg_bot,notifications,frontend

then copy the paths listed above into `service-template-<pinned-ref>/`.
