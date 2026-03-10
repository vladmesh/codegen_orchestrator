"""Phase 5: Deploy infrastructure prerequisites.

Mirrors the real deploy flow (deploy consumer → DevOps subgraph → GitHub Actions)
and checks every prerequisite is in place WITHOUT triggering a real deploy.

Real flow:
  1. deploy:queue → deploy consumer picks up job
  2. _allocate_resources → managed server + port
  3. _pre_check_server → SSH to server, verify state
  4. DevOps subgraph: env_analyzer → secret_resolver → readiness_check → deployer
  5. deployer: write secrets to GitHub → trigger deploy.yml → wait → smoke test

This test checks each link in that chain.
"""

from pathlib import Path
import subprocess

import pytest

ORCHESTRATOR_ROOT = "/home/vlad/projects/codegen_orchestrator"
SERVICE_TEMPLATE_ROOT = "/home/vlad/projects/service-template"


@pytest.fixture
async def managed_server(api_no_auth):
    """Get first managed server with active-ish status, or skip."""
    resp = await api_no_auth.get("/api/servers/")
    assert resp.status_code == 200, f"List servers failed: {resp.text}"
    servers = resp.json()

    active_statuses = {"active", "ready", "in_use"}
    managed = [s for s in servers if s.get("is_managed") and s.get("status") in active_statuses]
    if not managed:
        pytest.skip("No managed servers with active/ready/in_use status")
    return managed[0]


class TestManagedServer:
    """Step 2 of deploy flow: resource allocation needs a managed server."""

    @pytest.mark.asyncio
    async def test_managed_server_exists(self, api_no_auth):
        """At least one managed server with operational status in DB."""
        resp = await api_no_auth.get("/api/servers/")
        assert resp.status_code == 200
        servers = resp.json()

        active_statuses = {"active", "ready", "in_use"}
        managed = [s for s in servers if s.get("is_managed") and s.get("status") in active_statuses]
        assert len(managed) >= 1, (
            f"No managed servers with active status. "
            f"Found {len(servers)} total servers: "
            + ", ".join(f"{s['handle']}({s['status']})" for s in servers)
        )

    @pytest.mark.asyncio
    async def test_server_has_ssh_key(self, api_no_auth, managed_server):
        """Managed server has a decryptable SSH key (deployer fetches it at step 5)."""
        handle = managed_server["handle"]
        resp = await api_no_auth.get(f"/api/servers/{handle}/ssh-key")
        assert resp.status_code == 200, f"SSH key endpoint failed: {resp.text}"
        body = resp.json()
        key = body.get("ssh_key", "")
        assert key and len(key) > 100, (
            f"SSH key for {handle} is empty or too short ({len(key)} chars)"
        )

    def test_server_reachable_via_ssh(self, compose_exec, managed_server):
        """SSH from langgraph container to server works (deployer pre-check at step 3)."""
        ip = managed_server["public_ip"]
        user = managed_server.get("ssh_user", "root")
        handle = managed_server["handle"]

        # Mirrors _pre_check_server: fetch SSH key from API, connect to server
        script = (
            "import asyncio, sys, os\n"
            "sys.path.insert(0, '/app')\n"
            "async def main():\n"
            "    import httpx\n"
            "    api_url = os.environ.get('API_URL', 'http://api:8000')\n"
            "    async with httpx.AsyncClient(base_url=api_url, timeout=10) as client:\n"
            f"        resp = await client.get('/api/servers/{handle}/ssh-key')\n"
            "        if resp.status_code != 200:\n"
            "            print(f'FAILED:ssh-key-fetch:{resp.status_code}')\n"
            "            return\n"
            "        key = resp.json().get('ssh_key', '')\n"
            "    if not key.endswith('\\n'):\n"
            "        key += '\\n'\n"
            "    import tempfile, subprocess\n"
            "    with tempfile.NamedTemporaryFile(mode='w', suffix='.pem', delete=False) as f:\n"
            "        f.write(key)\n"
            "        key_path = f.name\n"
            "    os.chmod(key_path, 0o600)\n"
            "    result = subprocess.run(\n"
            "        ['ssh', '-i', key_path,\n"
            "         '-o', 'StrictHostKeyChecking=no',\n"
            "         '-o', 'ConnectTimeout=5',\n"
            "         '-o', 'BatchMode=yes',\n"
            f"         '{user}@{ip}', 'whoami'],\n"
            "        capture_output=True, text=True, timeout=15,\n"
            "    )\n"
            "    os.unlink(key_path)\n"
            "    if result.returncode == 0:\n"
            "        print(f'OK:{result.stdout.strip()}')\n"
            "    else:\n"
            "        print(f'FAILED:ssh:{result.stderr.strip()[:200]}')\n"
            "asyncio.run(main())\n"
        )
        result = subprocess.run(
            ["docker", "compose", "exec", "-T", "langgraph", "python", "-c", script],
            capture_output=True,
            text=True,
            timeout=30,
            cwd=ORCHESTRATOR_ROOT,
        )
        output = result.stdout.strip()
        assert output.startswith("OK:"), (
            f"SSH to {user}@{ip} ({handle}) failed: {output or result.stderr.strip()[:300]}"
        )


class TestPortAllocation:
    """Step 2 of deploy flow: allocator assigns next available port per module."""

    @pytest.mark.asyncio
    async def test_allocate_and_release_port(self, api_no_auth, managed_server):
        """Allocate next port, verify, then release — same as ensure_project_allocations()."""
        handle = managed_server["handle"]

        # Allocate (mirrors allocator.py: api_client.allocate_next_port)
        resp = await api_no_auth.post(
            f"/api/servers/{handle}/ports/allocate-next",
            json={"service_name": "live-test-deploy-infra", "start_port": 19000},
        )
        assert resp.status_code in (200, 201), f"Port allocation failed: {resp.text}"
        allocation = resp.json()
        alloc_id = allocation["id"]
        port = allocation["port"]
        assert port >= 19000, f"Allocated port {port} is below start_port 19000"

        try:
            # Verify it shows up in listings
            resp = await api_no_auth.get(f"/api/servers/{handle}/ports")
            assert resp.status_code == 200
            allocated_ports = [p["port"] for p in resp.json()]
            assert port in allocated_ports, f"Port {port} not found in server ports list"
        finally:
            # Always release — cleanup
            await api_no_auth.delete(f"/api/allocations/{alloc_id}")


class TestDeploySecrets:
    """Step 5 of deploy flow: deployer writes secrets to GitHub, needs env vars."""

    def test_deploy_workflow_template_exists(self):
        """service-template has deploy.yml.jinja — scaffolded repos will get deploy.yml."""
        template = Path(SERVICE_TEMPLATE_ROOT) / "template/.github/workflows/deploy.yml.jinja"
        assert template.exists(), (
            f"deploy.yml.jinja not found at {template}. "
            "Scaffolded repos won't have a deploy workflow."
        )
        content = template.read_text()
        assert "workflow_dispatch" in content, (
            "deploy.yml.jinja missing workflow_dispatch trigger — "
            "deployer won't be able to trigger it via API"
        )

    def test_langgraph_has_registry_env_vars(self, compose_exec):
        """langgraph container has ORCHESTRATOR_HOSTNAME + REGISTRY_* env vars.

        _write_deploy_secrets() reads these to push to GitHub Actions secrets.
        """
        env_vars = ["ORCHESTRATOR_HOSTNAME", "REGISTRY_USER", "REGISTRY_PASSWORD"]
        missing = []

        for var in env_vars:
            try:
                val = compose_exec("langgraph", f"printenv {var}")
                if not val.strip():
                    missing.append(f"{var}=<empty>")
            except RuntimeError:
                missing.append(f"{var}=<not set>")

        assert not missing, (
            f"langgraph container missing deploy env vars: {', '.join(missing)}. "
            "Deployer node (_write_deploy_secrets) will fail."
        )

    def test_secrets_encryption_key_available(self, compose_exec):
        """SECRETS_ENCRYPTION_KEY is set — needed to decrypt SSH keys for deploy."""
        try:
            val = compose_exec("langgraph", "printenv SECRETS_ENCRYPTION_KEY")
            assert val.strip(), "SECRETS_ENCRYPTION_KEY is empty"
        except RuntimeError:
            pytest.fail(
                "SECRETS_ENCRYPTION_KEY not set in langgraph container. "
                "SecretsCipher will fail when decrypting SSH keys for deploy."
            )


class TestDeployConsumer:
    """Step 1 of deploy flow: consumer must be alive to pick up deploy:queue messages."""

    def test_deploy_consumer_group_active(self, redis):
        """deploy:queue has consumer group with active consumer."""
        try:
            info = redis("XINFO", "GROUPS", "deploy:queue")
        except RuntimeError:
            pytest.fail("deploy:queue stream does not exist — deploy consumer never started?")

        assert "name" in info, "No consumer groups on deploy:queue"

        # Parse consumers count from XINFO output
        lines = info.split("\n")
        for line in lines:
            if "consumers" in line:
                parts = line.split()
                for j, part in enumerate(parts):
                    if part == "consumers" and j + 1 < len(parts):
                        try:
                            count = int(parts[j + 1])
                        except ValueError:
                            continue
                        assert count > 0, (
                            "deploy:queue has a consumer group but 0 consumers — "
                            "deploy-worker container may be down"
                        )
                        return
