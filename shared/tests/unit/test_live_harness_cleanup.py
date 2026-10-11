import asyncio
import base64
import shlex
from types import SimpleNamespace

import httpx
import pytest

from shared import live_harness_cleanup
from shared.clients.registry import DockerRegistryClient

REGISTRY_MEDIA_TYPES = (
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
)
OWNED_REPOSITORY = "org/owned-backend"
NEIGHBOUR_REPOSITORY = "org/owned-backend-other"
IMAGE_DIGEST = "sha256:" + "a" * 64


class RegistryPeer:
    """In-process registry with representation negotiation and scoped manifest storage."""

    def __init__(self, media_type):
        self.tags = {
            repository: {"latest": IMAGE_DIGEST, "sha-0573622": IMAGE_DIGEST}
            for repository in (OWNED_REPOSITORY, NEIGHBOUR_REPOSITORY)
        }
        self.manifests = {
            repository: {IMAGE_DIGEST: media_type}
            for repository in (OWNED_REPOSITORY, NEIGHBOUR_REPOSITORY)
        }
        self.requests = []
        self.deletes = []
        self.retain_manifest = False
        self.delete_status = 202
        self.listing_after_delete = "stale"
        self.remove_tags = False
        self.omit_digest = False
        self.read_failure = None
        self.fail_after_delete = False
        self.failure_boundary = "manifests"

    def __call__(self, request):
        assert request.url.host == "registry.example.com"
        assert request.headers["Authorization"] == "Basic dXNlcjpwYXNzd29yZA=="
        self.requests.append(request)
        repository, boundary, reference = request.url.path.removeprefix("/v2/").rsplit("/", 2)
        if request.method == "DELETE":
            assert boundary == "manifests"
            self.deletes.append((repository, reference))
            if not self.retain_manifest:
                self.manifests[repository].pop(reference, None)
            if self.remove_tags:
                self.tags[repository].clear()
            return httpx.Response(self.delete_status)
        assert request.method == "GET"
        if (
            self.read_failure is not None
            and boundary == self.failure_boundary
            and (not self.fail_after_delete or self.deletes)
        ):
            if self.read_failure == "transport":
                raise httpx.ConnectError("controlled unreadable registry", request=request)
            return httpx.Response(self.read_failure)
        if repository not in self.tags:
            return httpx.Response(404)
        if boundary == "tags":
            if self.deletes and self.listing_after_delete == "404":
                return httpx.Response(404)
            tags = (
                []
                if self.deletes and self.listing_after_delete == "empty"
                else list(self.tags[repository])
            )
            return httpx.Response(200, json={"name": repository, "tags": tags})
        assert boundary == "manifests"
        digest = self.tags[repository].get(reference, reference)
        media_type = self.manifests[repository].get(digest)
        if media_type is None or media_type not in request.headers.get("Accept", "").split(", "):
            return httpx.Response(404, json={"errors": [{"code": "MANIFEST_UNKNOWN"}]})
        headers = {"Content-Type": media_type}
        if not self.omit_digest:
            headers["Docker-Content-Digest"] = digest
        return httpx.Response(
            200, headers=headers, json={"schemaVersion": 2, "mediaType": media_type}
        )


@pytest.fixture
def registry_peer(monkeypatch):
    peer = RegistryPeer(REGISTRY_MEDIA_TYPES[0])
    monkeypatch.setenv("ORCHESTRATOR_HOSTNAME", "registry.example.com")
    monkeypatch.setenv("REGISTRY_USER", "user")
    monkeypatch.setenv("REGISTRY_PASSWORD", "password")
    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        live_harness_cleanup.httpx,
        "AsyncClient",
        lambda **kwargs: real_client(transport=httpx.MockTransport(peer), **kwargs),
    )
    return peer


@pytest.mark.parametrize("media_type", REGISTRY_MEDIA_TYPES)
@pytest.mark.asyncio
async def test_registry_cleanup_deletes_shared_digest_and_proves_absence(registry_peer, media_type):
    peer = registry_peer
    peer.manifests[OWNED_REPOSITORY][IMAGE_DIGEST] = media_type
    client = DockerRegistryClient()
    assert await client.manifest_digest(OWNED_REPOSITORY, "latest") == IMAGE_DIGEST

    await live_harness_cleanup.cleanup_registry_repository(repository=OWNED_REPOSITORY)

    assert peer.deletes == [(OWNED_REPOSITORY, IMAGE_DIGEST)]
    # The stale tag listing must lead to authoritative manifest readback, including the digest.
    delete_index = next(i for i, request in enumerate(peer.requests) if request.method == "DELETE")
    verified = {request.url.path for request in peer.requests[delete_index + 1 :]}
    assert {
        f"/v2/{OWNED_REPOSITORY}/manifests/{reference}"
        for reference in ("latest", "sha-0573622", IMAGE_DIGEST)
    } <= verified
    assert await client.manifest_digest(OWNED_REPOSITORY, "latest") is None
    assert await client.manifest_digest(OWNED_REPOSITORY, IMAGE_DIGEST) is None
    assert await client.manifest_digest(NEIGHBOUR_REPOSITORY, "latest") == IMAGE_DIGEST


@pytest.mark.parametrize("media_type", REGISTRY_MEDIA_TYPES)
@pytest.mark.asyncio
async def test_registry_residue_finds_live_images_then_observes_cleanup(registry_peer, media_type):
    registry_peer.manifests[OWNED_REPOSITORY][IMAGE_DIGEST] = media_type
    assert await live_harness_cleanup._registry_repositories_residue([OWNED_REPOSITORY]) == {
        "findings": [f"{OWNED_REPOSITORY}:latest", f"{OWNED_REPOSITORY}:sha-0573622"]
    }
    await live_harness_cleanup.cleanup_registry_repository(repository=OWNED_REPOSITORY)
    assert await live_harness_cleanup._registry_repositories_residue([OWNED_REPOSITORY]) == {
        "findings": []
    }


@pytest.mark.parametrize("media_type", REGISTRY_MEDIA_TYPES)
@pytest.mark.parametrize("delete_status", [202, 404])
@pytest.mark.asyncio
async def test_registry_cleanup_refuses_retained_manifest(registry_peer, media_type, delete_status):
    peer = registry_peer
    peer.manifests[OWNED_REPOSITORY][IMAGE_DIGEST] = media_type
    peer.retain_manifest = True
    peer.delete_status = delete_status
    with pytest.raises(RuntimeError, match="remain"):
        await live_harness_cleanup.cleanup_registry_repository(repository=OWNED_REPOSITORY)
    assert peer.deletes == [(OWNED_REPOSITORY, IMAGE_DIGEST)]
    assert await live_harness_cleanup._registry_repositories_residue([OWNED_REPOSITORY]) == {
        "findings": [f"{OWNED_REPOSITORY}:latest", f"{OWNED_REPOSITORY}:sha-0573622"]
    }


@pytest.mark.parametrize("listing", ["empty", "404"])
@pytest.mark.parametrize("remove_tags", [False, True])
@pytest.mark.asyncio
async def test_registry_cleanup_checks_known_references_when_listing_disappears(
    registry_peer, listing, remove_tags
):
    peer = registry_peer
    peer.retain_manifest = True
    peer.listing_after_delete = listing
    peer.remove_tags = remove_tags
    with pytest.raises(RuntimeError, match="remain"):
        await live_harness_cleanup.cleanup_registry_repository(repository=OWNED_REPOSITORY)
    assert peer.deletes == [(OWNED_REPOSITORY, IMAGE_DIGEST)]
    assert (
        await DockerRegistryClient().manifest_digest(OWNED_REPOSITORY, IMAGE_DIGEST) == IMAGE_DIGEST
    )


@pytest.mark.parametrize("listing", ["empty", "404"])
@pytest.mark.asyncio
async def test_registry_cleanup_accepts_absence_after_digest_deletion(registry_peer, listing):
    registry_peer.listing_after_delete = listing
    registry_peer.remove_tags = True
    await live_harness_cleanup.cleanup_registry_repository(repository=OWNED_REPOSITORY)
    assert registry_peer.deletes == [(OWNED_REPOSITORY, IMAGE_DIGEST)]
    assert await DockerRegistryClient().manifest_digest(OWNED_REPOSITORY, IMAGE_DIGEST) is None


@pytest.mark.asyncio
async def test_registry_cleanup_and_residue_accept_genuinely_absent_repositories(registry_peer):
    registry_peer.manifests[OWNED_REPOSITORY].clear()
    registry_peer.tags["org/empty"] = {}
    registry_peer.manifests["org/empty"] = {}
    for repository in (OWNED_REPOSITORY, "org/empty", "org/never-published"):
        await live_harness_cleanup.cleanup_registry_repository(repository=repository)
        assert await live_harness_cleanup._registry_repositories_residue([repository]) == {
            "findings": []
        }
    assert registry_peer.deletes == []


@pytest.mark.asyncio
async def test_registry_missing_digest_is_unreadable_and_cannot_be_deleted(registry_peer):
    registry_peer.omit_digest = True
    with pytest.raises(RuntimeError, match="digest missing"):
        await live_harness_cleanup.cleanup_registry_repository(repository=OWNED_REPOSITORY)
    assert registry_peer.deletes == []
    assert await live_harness_cleanup._registry_repositories_residue([OWNED_REPOSITORY]) == {
        "error": f"manifest digest missing for {OWNED_REPOSITORY}:latest"
    }


@pytest.mark.parametrize("failure", [401, 403, 503, "transport"])
@pytest.mark.parametrize("boundary", ["tags", "manifests"])
@pytest.mark.asyncio
async def test_registry_unreadable_discovery_cannot_report_absence(
    registry_peer, failure, boundary
):
    registry_peer.read_failure = failure
    registry_peer.failure_boundary = boundary
    with pytest.raises(httpx.HTTPError):
        await live_harness_cleanup.cleanup_registry_repository(repository=OWNED_REPOSITORY)
    assert registry_peer.deletes == []
    residue = await live_harness_cleanup._registry_repositories_residue([OWNED_REPOSITORY])
    assert set(residue) == {"error"}
    assert residue["error"]


@pytest.mark.parametrize("boundary", ["tags", "manifests"])
@pytest.mark.asyncio
async def test_registry_unreadable_verification_cannot_report_cleanup_success(
    registry_peer, boundary
):
    registry_peer.read_failure = 503
    registry_peer.failure_boundary = boundary
    registry_peer.fail_after_delete = True
    with pytest.raises(httpx.HTTPStatusError):
        await live_harness_cleanup.cleanup_registry_repository(repository=OWNED_REPOSITORY)
    assert registry_peer.deletes == [(OWNED_REPOSITORY, IMAGE_DIGEST)]
    assert "error" in await live_harness_cleanup._registry_repositories_residue([OWNED_REPOSITORY])


def test_remote_cleanup_command_keeps_project_name_as_argv() -> None:
    project_name = "live-test'\nrm -rf /"

    command = live_harness_cleanup.build_remote_cleanup_command(project_name)

    assert shlex.split(command) == ["sh", "-s", "--", project_name, "/opt/services"]


def test_registry_cleanup_uses_https_for_bare_registry_host(monkeypatch) -> None:
    requested_urls = []

    class Response:
        status_code = live_harness_cleanup.HTTP_NOT_FOUND

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, url, **kwargs):
            requested_urls.append(url)
            return Response()

    monkeypatch.setenv("ORCHESTRATOR_HOSTNAME", "registry.example.com")
    monkeypatch.setenv("REGISTRY_USER", "user")
    monkeypatch.setenv("REGISTRY_PASSWORD", "password")
    monkeypatch.setattr(live_harness_cleanup.httpx, "AsyncClient", lambda **kwargs: Client())

    asyncio.run(
        live_harness_cleanup.cleanup_registry_repository(
            repository="project-factory-organization/owned-repository-backend"
        )
    )

    assert requested_urls == [
        "https://registry.example.com/v2/"
        "project-factory-organization/owned-repository-backend/tags/list"
    ]


def test_registry_cleanup_fails_without_credentials(monkeypatch) -> None:
    monkeypatch.delenv("ORCHESTRATOR_HOSTNAME", raising=False)
    monkeypatch.setenv("REGISTRY_USER", "user")
    monkeypatch.setenv("REGISTRY_PASSWORD", "password")

    with pytest.raises(RuntimeError, match="credentials are not configured"):
        asyncio.run(live_harness_cleanup.cleanup_registry_repository(repository="repo"))


def test_merge_file_set_probe_reads_paths_and_the_two_legacy_edit_targets(
    monkeypatch, capsys
) -> None:
    class GitHub:
        async def get_repo(self, *_args):
            return SimpleNamespace(default_branch="main")

        async def get_token(self, *_args):
            return "token"

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, url, **kwargs):
            request = httpx.Request("GET", url)
            if url.endswith("/commits/merge"):
                return httpx.Response(
                    200,
                    json={
                        "sha": "merge",
                        "parents": [{"sha": "parent"}],
                        "files": [
                            {"filename": "Makefile"},
                            {"filename": "AGENTS.md"},
                            {"filename": "product.py"},
                        ],
                    },
                    request=request,
                )
            if "/compare/main...merge" in url:
                return httpx.Response(200, json={"status": "behind"}, request=request)
            path = url.rsplit("/contents/", 1)[1]
            ref = kwargs["params"]["ref"]
            contents = {
                ("Makefile", "merge"): "all:\n\t@true\n",
                ("AGENTS.md", "merge"): "# Product instructions\n",
                ("AGENTS.md", "parent"): "# Product instructions\n",
            }
            return httpx.Response(
                200,
                json={
                    "encoding": "base64",
                    "content": base64.b64encode(contents[path, ref].encode()).decode(),
                },
                request=request,
            )

    monkeypatch.setattr(live_harness_cleanup, "GitHubAppClient", GitHub)
    monkeypatch.setattr(live_harness_cleanup.httpx, "AsyncClient", lambda **_kwargs: Client())

    payload = asyncio.run(
        live_harness_cleanup.probe_merge_file_set(
            owner="org", repo="repo", merge_commit_sha="merge"
        )
    )

    assert payload == {
        "merge_commit_sha": "merge",
        "default_branch": "main",
        "merged_into_default_branch": True,
        "parent_shas": ["parent"],
        "changed_paths": ["AGENTS.md", "Makefile", "product.py"],
        "file_contents": {
            "Makefile": "all:\n\t@true\n",
            "AGENTS.md": "# Product instructions\n",
        },
        "parent_file_contents": {"AGENTS.md": "# Product instructions\n"},
    }
    assert live_harness_cleanup.MERGE_FILE_SET_PROBE_MARKER in capsys.readouterr().out
