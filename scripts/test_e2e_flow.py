import asyncio
import logging
import secrets
import string
import sys

import httpx

# Configure logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("e2e_test")

API_URL = "http://localhost:8000"


def generate_random_string(length=8):
    return "".join(secrets.choice(string.ascii_lowercase + string.digits) for _ in range(length))


async def wait_for_api():
    """Wait for API to be healthy."""
    async with httpx.AsyncClient() as client:
        for _ in range(30):
            try:
                response = await client.get(f"{API_URL}/health")
                if response.status_code == httpx.codes.OK:
                    logger.info("✅ API is healthy")
                    return True
            except httpx.ConnectError:
                pass
            await asyncio.sleep(1)
            logger.info("Waiting for API...")
    return False


async def test_projects(client):
    """Test Project CRUD."""
    logger.info("🧪 Testing Projects...")

    # List projects (should have synced ones)
    response = await client.get(f"{API_URL}/api/projects/")
    projects = response.json()
    logger.info(f"Found {len(projects)} synced projects")

    # Create new project
    project_id = f"test-project-{generate_random_string()}"
    new_project = {
        "id": project_id,
        "name": "E2E Test Project",
        "status": "draft",
        "config": {"description": "Created by E2E test"},
    }

    response = await client.post(f"{API_URL}/api/projects/", json=new_project)
    if response.status_code != httpx.codes.CREATED:
        logger.error(f"Failed to create project: {response.text}")
        return False

    logger.info(f"✅ Created project {project_id}")

    # Verify it exists
    response = await client.get(f"{API_URL}/api/projects/{project_id}")
    if response.status_code != httpx.codes.OK:
        logger.error("Failed to retrieve created project")
        return False

    logger.info("✅ Verified project existence")
    return True


async def test_servers(client):
    """Test Server CRUD."""
    logger.info("🧪 Testing Servers...")

    # List servers (should have synced ones)
    response = await client.get(f"{API_URL}/api/servers/")
    servers = response.json()
    logger.info(f"Found {len(servers)} synced servers")

    if not servers:
        logger.warning("⚠️ No synced servers found! (Might be expected if empty account)")

    # Create dummy server
    handle = f"test-vps-{generate_random_string()}"
    new_server = {
        "handle": handle,
        "host": "test.host",
        "public_ip": "1.2.3.4",
        "ssh_user": "root",
        "ssh_key": "ssh-rsa mock",
        "capacity_cpu": 1,
        "capacity_ram_mb": 1024,
        "capacity_disk_mb": 10240,
        "labels": {"test": "true"},
    }

    response = await client.post(f"{API_URL}/api/servers/", json=new_server)
    if response.status_code != httpx.codes.CREATED:
        logger.error(f"Failed to create server: {response.text}")
        return False

    logger.info(f"✅ Created server {handle}")
    return True


async def main():
    if not await wait_for_api():
        logger.error("❌ API not available")
        sys.exit(1)

    async with httpx.AsyncClient() as client:
        success_projects = await test_projects(client)
        success_servers = await test_servers(client)

        if success_projects and success_servers:
            logger.info("🎉 All E2E tests PASSED!")
            sys.exit(0)
        else:
            logger.error("❌ Some E2E tests FAILED")
            sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
