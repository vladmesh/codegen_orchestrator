"""Fixed read-only stand provenance probe, using the existing App and owned SSH target."""

import argparse
import asyncio
import hashlib
import json
import shlex

import httpx
import structlog

from shared.clients.github import GitHubAppClient
from shared.clients.registry import DockerRegistryClient, parse_image_reference
from shared.live_harness_cleanup import _file_contents_at, _resolve_ssh_targets, _run_over_ssh
from shared.log_config import setup_logging
from src.clients.api import api_client

OWNED_FILES = (
    "services/backend/src/app/api/routers/notes.py",
    "services/backend/src/app/api/router.py",
    "services/tg_bot/src/handlers/notes.py",
    "services/tg_bot/src/main.py",
)

CONTAINER_READ = """import hashlib, importlib.metadata as m, json
from pathlib import Path
root = Path('/app')
result = {'distributions': {}, 'hashes': {}}
for name in ('codegen-kit-reminders', 'codegen-kit-textparse'):
    try:
        result['distributions'][name] = m.version(name)
    except m.PackageNotFoundError:
        result['distributions'][name] = None
for name in (
    'services/backend/src/app/api/routers/notes.py',
    'services/backend/src/app/api/router.py',
    'services/tg_bot/src/handlers/notes.py', 'services/tg_bot/src/main.py',
    'services/tg_bot/bindings/reminders.yaml',
    'codegen_kit/_active_packages.py', 'codegen_kit/__init__.py',
):
    file = root / name
    if file.is_file():
        result['hashes'][name] = hashlib.sha256(file.read_bytes()).hexdigest()
if (root / 'codegen_kit/__init__.py').is_file():
    import codegen_kit
    result['core'] = codegen_kit.CORE_VERSION
print(json.dumps(result))
"""

COMPONENT_READ = """
result['component'] = {}
binding = root / ('services/tg_bot/bindings/' + component['name'] + '.yaml')
if binding.is_file():
    result['component']['binding_sha256'] = hashlib.sha256(binding.read_bytes()).hexdigest()
if (root / 'services/backend/src/main.py').is_file():
    import yaml
    from importlib.resources import files
    from codegen_kit._active_packages import ACTIVE_PACKAGES
    result['component']['active'] = next(
        item for item in ACTIVE_PACKAGES if item['name'] == component['name'])
    result['component']['version'] = m.version(component['distribution'])
    manifest_bytes = files(component['module']).joinpath('package.yaml').read_bytes()
    result['component']['manifest_sha256'] = hashlib.sha256(manifest_bytes).hexdigest()
    result['component']['manifest'] = yaml.safe_load(manifest_bytes)
    result['component']['contract'] = yaml.safe_load(
        (root / 'services/backend/env.contract.yaml').read_text())
"""


async def read_deployment(project_name, server_handle, *, component=None):
    targets = await _resolve_ssh_targets(str(api_client.base_url), server_handle)
    if len(targets) != 1:
        raise RuntimeError("deployment does not resolve to exactly one owned target")
    destination, key, _ = targets[0]
    result = {}
    for service in ("backend", "tg_bot"):
        script = CONTAINER_READ
        if component is not None:
            query = json.dumps(component)
            script = (
                script.replace("print(json.dumps(result))", "")
                + f"\ncomponent = json.loads({query!r})\n"
                + COMPONENT_READ
                + "\nprint(json.dumps(result))\n"
            )
        listing = _run_over_ssh(
            destination,
            key,
            shlex.join(
                [
                    "docker",
                    "ps",
                    "--no-trunc",
                    "--filter",
                    f"label=com.docker.compose.project={project_name}",
                    "--filter",
                    f"label=com.docker.compose.service={service}",
                    "--format",
                    "{{.ID}}",
                ]
            ),
            "",
            timeout=30,
        )
        ids = listing.stdout.split()
        if listing.returncode or len(ids) != 1:
            raise RuntimeError(f"expected one running {service} container")
        container_id = ids[0]
        inspect = _run_over_ssh(
            destination,
            key,
            shlex.join(
                [
                    "docker",
                    "inspect",
                    "--format",
                    "{{json .Image}} {{json .Config.Image}}",
                    container_id,
                ]
            ),
            "",
            timeout=30,
        )
        if inspect.returncode:
            raise RuntimeError(f"{service} image read failed")
        image_id, reference = map(json.loads, inspect.stdout.split())
        parsed = parse_image_reference(reference)
        registry_digest = await DockerRegistryClient().manifest_digest(
            parsed.repository, parsed.tag
        )
        if registry_digest is None:
            raise RuntimeError(f"{service} deployed image tag is absent from the registry")
        image = _run_over_ssh(
            destination,
            key,
            shlex.join(
                [
                    "docker",
                    "image",
                    "inspect",
                    "--format",
                    "{{json .RepoDigests}}",
                    image_id,
                ]
            ),
            "",
            timeout=30,
        )
        read = _run_over_ssh(
            destination,
            key,
            shlex.join(
                [
                    "docker",
                    "exec",
                    container_id,
                    "python",
                    "-c",
                    script,
                ]
            ),
            "",
            timeout=30,
        )
        if image.returncode or read.returncode:
            raise RuntimeError(f"{service} fixed artifact read failed")
        result[service] = {
            "container_id": container_id,
            "image_id": image_id,
            "reference": reference,
            "registry_digest": registry_digest,
            "digests": json.loads(image.stdout),
            **json.loads(read.stdout),
        }
    return result


async def read_publication(owner, repo, base, head, story=None, merge=None, pr=None):
    async with GitHubAppClient() as gh:
        token = await gh.get_org_token(owner)
        headers = {"Authorization": f"token {token}", "Accept": "application/vnd.github+json"}
        api = f"https://api.github.com/repos/{owner}/{repo}"
        async with httpx.AsyncClient(timeout=30) as client:

            async def read(path):
                response = await client.get(f"{api}/{path}", headers=headers)
                response.raise_for_status()
                return response.json()

            compare = await read(f"compare/{base}...{head}")
            merge_ci = (
                (await read(f"actions/runs?head_sha={merge}&per_page=100"))["workflow_runs"]
                if merge
                else []
            )
            pull_request = await read(f"pulls/{pr}") if pr else None
            branch_head = (
                (await read(f"git/ref/heads/story/{story}"))["object"]["sha"] if story else None
            )
            files = {}
            for path in OWNED_FILES:
                content = await _file_contents_at(
                    client, api=api, headers=headers, path=path, ref=head
                )
                if content is None:
                    raise RuntimeError(f"owned notes file absent: {path}")
                files[path] = hashlib.sha256(content.encode()).hexdigest()
            return {
                "base": base,
                "head": head,
                "merge_base": compare["merge_base_commit"]["sha"],
                "commits": [commit["sha"] for commit in compare["commits"]],
                "owned_hashes": files,
                "branch_head": branch_head,
                "merge_sha": merge,
                "merge_ci": [
                    {
                        key: run[key]
                        for key in (
                            "id",
                            "head_sha",
                            "status",
                            "conclusion",
                            "event",
                            "path",
                            "html_url",
                        )
                    }
                    for run in merge_ci
                ],
                "pull_request": {
                    key: pull_request[key]
                    for key in ("number", "merged", "merge_commit_sha", "html_url")
                }
                | {"head": {key: pull_request["head"][key] for key in ("sha", "ref")}}
                if pull_request
                else None,
            }


async def invoke(args):
    try:
        deployment = await read_deployment(args.project_name, args.server)
        publication = await read_publication(
            args.owner, args.repo, args.base, args.head, args.story, args.merge, args.pr
        )
        structlog.get_logger().info(
            "mechanical_readback", result={"deployment": deployment, "publication": publication}
        )
    finally:
        await api_client.close()


def main():
    setup_logging(service_name="mechanical_readback", log_format="json")
    parser = argparse.ArgumentParser()
    for name in ("project-name", "server", "owner", "repo", "base", "head"):
        parser.add_argument(f"--{name}", required=True)
    parser.add_argument("--story")
    parser.add_argument("--merge")
    parser.add_argument("--pr", type=int)
    asyncio.run(invoke(parser.parse_args()))


if __name__ == "__main__":
    main()
