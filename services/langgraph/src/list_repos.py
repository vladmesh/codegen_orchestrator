import asyncio
import os
import sys

# Add the service directory to sys.path to allow imports
sys.path.append(os.path.abspath("services/langgraph/src"))

import httpx

from shared.clients.github import GitHubAppClient
from shared.logging_config import get_logger, setup_logging

setup_logging(service_name="langgraph")
logger = get_logger(__name__)


async def list_repos():
    client = GitHubAppClient()

    try:
        # Get the first org installation
        try:
            installation = await client.get_first_org_installation()
        except RuntimeError as e:
            logger.error("github_org_installation_failed", error=str(e))
            return

        org = installation["org"]
        logger.info("github_org_target", org=org)

        # Get token
        token = await client.get_org_token(org)

        headers = {
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github+json",
        }

        async with httpx.AsyncClient() as http_client:
            resp = await http_client.get(
                f"https://api.github.com/orgs/{org}/repos?per_page=100",
                headers=headers,
            )
            resp.raise_for_status()
            repos = resp.json()

            print(f"\nScanning repositories in {org}:")
            found_potential = False
            for repo in repos:
                name = repo["name"]
                full_name = repo["full_name"]
                # Filter for palindrome related
                if "palindrome" in name.lower():
                    print(f"FOUND: {name} ({full_name})")
                    found_potential = True
                else:
                    # Print others just in case but less prominent
                    # print(f"  - {name}")
                    pass

            if not found_potential:
                print("No repositories matching 'palindrome' found.")
                print("All repos:")
                for repo in repos:
                    print(f"  - {repo['name']}")

    except Exception:
        logger.exception("github_repo_listing_failed")


if __name__ == "__main__":
    asyncio.run(list_repos())
