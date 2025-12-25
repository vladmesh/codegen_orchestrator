#!/usr/bin/env python3
import json
import os
import sys
import urllib.error
import urllib.request


def get_inventory():
    api_url = os.environ.get("ORCHESTRATOR_API_URL")
    api_token = os.environ.get("ORCHESTRATOR_API_TOKEN")

    if not api_url:
        # Fallback for empty inventory to avoid breaking if var not set
        return {"_meta": {"hostvars": {}}}

    headers = {"Authorization": f"Bearer {api_token}", "Content-Type": "application/json"}

    try:
        req = urllib.request.Request(f"{api_url}/api/servers/", headers=headers)  # noqa: S310
        with urllib.request.urlopen(req) as response:  # noqa: S310
            data = json.loads(response.read().decode())
    except urllib.error.URLError as e:
        # In case of API failure, print to stderr but return empty inventory to
        # not break ansible completely
        print(f"Error fetching inventory: {e}", file=sys.stderr)
        return {"_meta": {"hostvars": {}}}

    inventory = {
        "all": {"hosts": [], "children": ["ungrouped"]},
        "ungrouped": {"hosts": []},
        "_meta": {"hostvars": {}},
    }

    for server in data:
        hostname = server.get("hostname") or server.get("ip_address")
        if not hostname:
            continue

        ip = server.get("ip_address")
        project_id = server.get("project_id")
        provisioned = server.get("provisioned", False)

        # Add to all
        inventory["all"]["hosts"].append(hostname)

        # Hostvars
        inventory["_meta"]["hostvars"][hostname] = {
            "ansible_host": ip,
            "ansible_user": server.get("user", "root"),
            # Add other necessary vars
        }

        # Groups
        if not provisioned:
            inventory["ungrouped"]["hosts"].append(hostname)

        if project_id:
            group_name = f"project_{project_id}"
            if group_name not in inventory:
                inventory[group_name] = {"hosts": []}
            inventory[group_name]["hosts"].append(hostname)

    return inventory


if __name__ == "__main__":
    if len(sys.argv) == 2 and (sys.argv[1] == "--list"):  # noqa: PLR2004
        print(json.dumps(get_inventory(), indent=2))
    elif len(sys.argv) == 2 and (sys.argv[1] == "--host"):  # noqa: PLR2004
        print(json.dumps({}))  # Not needed if _meta is used
    else:
        print("Usage: api_inventory.py --list")
        sys.exit(1)
