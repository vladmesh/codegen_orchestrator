"""DevOps agent node.

Orchestrates the deployment of the application using Ansible.
Runs after the Developer agent/worker has completed implementation.
"""

import logging
import os
import subprocess
import tempfile
from typing import Any

from langchain_core.messages import AIMessage

from ..clients.github import GitHubAppClient

logger = logging.getLogger(__name__)


async def run(state: dict) -> dict:
    """Run devops agent.
    
    1. Extract deployment info (repo, resources).
    2. Get GitHub token.
    3. Run Ansible playbook to deploy.
    4. Return status.
    """
    repo_info = state.get("repo_info", {})
    project_spec = state.get("project_spec", {})
    allocated_resources = state.get("allocated_resources", {})
    
    if not repo_info:
        return {
            "errors": state.get("errors", []) + ["No repository info for deployment"],
            "messages": [AIMessage(content="❌ No repository info found. Cannot deploy.")],
        }
    
    # Identify the port and server from allocated resources
    # allocated_resources keys are like "server_handle:port"
    # value is dict with keys: port, server_handle, server_ip
    
    target_resource = None
    target_server_ip = None
    target_port = None
    
    # Simple heuristic: take the first allocated http/service resource
    # In reality we might filter by "is_web_service" or similar if we had that metadata
    for key, res in allocated_resources.items():
        if res.get("port") and res.get("server_ip"):
            target_resource = res
            target_server_ip = res.get("server_ip")
            target_port = res.get("port")
            break
            
    if not target_resource:
        return {
            "errors": state.get("errors", []) + ["No allocated server resource found"],
            "messages": [AIMessage(content="❌ No server resources allocated. Cannot deploy.")],
        }

    repo_full_name = repo_info.get("full_name")
    project_name = project_spec.get("name", "project").replace(" ", "_").lower()
    
    # Get token for deployment (pulling images, getting compose file)
    github_client = GitHubAppClient()
    owner, repo = repo_full_name.split("/")
    
    try:
        token = await github_client.get_token(owner, repo)
    except Exception as e:
        logger.exception(f"Failed to get GitHub token: {e}")
        return {
             "errors": state.get("errors", []) + [f"Failed to get GitHub token: {e}"],
             "messages": [AIMessage(content=f"❌ Failed to get GitHub token: {e}")],
        }

    # Prepare Ansible Playbook execution
    playbook_path = "/app/services/infrastructure/ansible/playbooks/deploy_project.yml"
    # Note: In the container, paths will depend on how we mount/copy things.
    # The Dockerfile copies `services/langgraph/src` to `./src`.
    # But where are the playbooks?
    # We might need to adjust the Dockerfile to copy ansible playbooks too or assume they are mounted.
    # PROD FIX: The Dockerfile should copy infrastructure if we want to run ansible from inside.
    # Currently it assumes `services/langgraph/src` is copied.
    # I should update Dockerfile to copy `services/infrastructure` to `/app/services/infrastructure`
    
    # For now, let's assume the path is correct internally if we fix Dockerfile.
    
    # Construct inventory dynamically
    inventory_content = f"{target_server_ip} ansible_user=root ansible_ssh_private_key_file=/root/.ssh/id_rsa ansible_ssh_common_args='-o StrictHostKeyChecking=no'"
    
    # Create temp inventory file
    with tempfile.NamedTemporaryFile(mode='w', delete=False) as inventory_file:
        inventory_file.write(inventory_content)
        inventory_path = inventory_file.name

    extra_vars = (
        f"project_name={project_name} "
        f"repo_full_name={repo_full_name} "
        f"github_token={token} "
        f"port={target_port}"
    )
    
    cmd = [
        "ansible-playbook",
        "-i", inventory_path,
        playbook_path,
        "--extra-vars", extra_vars
    ]
    
    logger.info(f"Running deployment for {repo_full_name} on {target_server_ip}:{target_port}")
    
    try:
        # Run Ansible
        # Use simple subprocess, capturing output
        process = subprocess.run(
            cmd, 
            capture_output=True, 
            text=True, 
            timeout=300 # 5 minutes timeout
        )
        
        if process.returncode == 0:
            deployed_url = f"http://{target_server_ip}:{target_port}"
            message = f"""✅ Deployment successful!
            
Project: {project_name}
URL: {deployed_url}
Server: {target_server_ip}
Port: {target_port}
"""
            return {
                "deployed_url": deployed_url,
                "messages": [AIMessage(content=message)],
                "current_agent": "devops"
            }
        else:
            logger.error(f"Ansible failed: {process.stderr}")
            return {
                "errors": state.get("errors", []) + ["Deployment failed"],
                "messages": [AIMessage(content=f"❌ Deployment failed:\n\n{process.stderr[-500:]}\n\nSTDOUT:\n{process.stdout[-200:]}")],
                "current_agent": "devops"
            }

    except Exception as e:
        logger.exception("Deployment exception")
        return {
            "errors": state.get("errors", []) + [f"Deployment exception: {e}"],
            "messages": [AIMessage(content=f"❌ Deployment crashed: {e}")],
            "current_agent": "devops"
        }
    finally:
        # Cleanup inventory
        if os.path.exists(inventory_path):
            os.remove(inventory_path)
